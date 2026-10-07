//! Local Worker-to-launcher specification; no execution authority or host paths.
//!
//! Validation is lexical only. The launcher must subsequently resolve artifacts
//! within their task, enforce filesystem containment, and apply all isolation.

use kf_contracts::SchemaVersion;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

pub const MAX_SPEC_BYTES: u64 = 65_536;
const MIB: u64 = 1024 * 1024;
const MAX_PATH_BYTES: usize = 4096;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionProfile {
    Native,
    CompileC,
    CompileCpp,
}

impl ExecutionProfile {
    /// Trusted image-local compiler tools only; never inherit the host PATH.
    pub fn compiler_search_path(self) -> Option<&'static str> {
        match self {
            Self::Native => None,
            Self::CompileC | Self::CompileCpp => Some("/usr/bin"),
        }
    }
}

/// Every field is explicit: omission cannot silently remove a resource limit.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResourceLimits {
    pub cpu_time_us: u64,
    pub wall_time_us: u64,
    pub memory_bytes: u64,
    pub stack_bytes: u64,
    pub processes: u32,
    pub open_files: u32,
    pub stdout_bytes: u64,
    pub stderr_bytes: u64,
    pub file_bytes: u64,
    pub workdir_bytes: u64,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        Self {
            cpu_time_us: 1_000_000,
            wall_time_us: 3_000_000,
            memory_bytes: 64 * MIB,
            stack_bytes: 8 * MIB,
            processes: 8,
            open_files: 64,
            stdout_bytes: MIB,
            stderr_bytes: MIB,
            file_bytes: 16 * MIB,
            workdir_bytes: 64 * MIB,
        }
    }
}

impl ResourceLimits {
    pub fn for_profile(profile: ExecutionProfile) -> Self {
        match profile {
            ExecutionProfile::Native => Self::default(),
            ExecutionProfile::CompileC | ExecutionProfile::CompileCpp => Self {
                cpu_time_us: 10_000_000,
                wall_time_us: 30_000_000,
                memory_bytes: 512 * MIB,
                stack_bytes: 16 * MIB,
                processes: 32,
                open_files: 128,
                file_bytes: 64 * MIB,
                workdir_bytes: 256 * MIB,
                ..Self::default()
            },
        }
    }

    fn ceilings() -> Self {
        Self {
            cpu_time_us: 60_000_000,
            wall_time_us: 120_000_000,
            memory_bytes: 2048 * MIB,
            stack_bytes: 256 * MIB,
            processes: 128,
            open_files: 1024,
            stdout_bytes: 16 * MIB,
            stderr_bytes: 16 * MIB,
            file_bytes: 64 * MIB,
            workdir_bytes: 256 * MIB,
        }
    }
}

/// Read-only input at a canonical relative path beneath /work.
/// The artifact provider must independently check ownership and file type.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputArtifact {
    pub artifact_id: String,
    pub path: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionSpec {
    pub schema_version: SchemaVersion,
    pub run_id: String,
    pub profile: ExecutionProfile,
    pub argv: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Canonical relative directory beneath /work; "." selects /work itself.
    pub cwd: String,
    #[serde(default)]
    pub inputs: Vec<InputArtifact>,
    pub stdin_artifact: Option<String>,
    #[serde(default)]
    pub outputs: Vec<String>,
    pub limits: ResourceLimits,
}

/// Trusted operator policy, deliberately absent from the request schema.
#[derive(Clone, Debug)]
pub struct SpecPolicy {
    limits_ceiling: ResourceLimits,
}

impl Default for SpecPolicy {
    fn default() -> Self {
        Self {
            limits_ceiling: ResourceLimits::ceilings(),
        }
    }
}

impl SpecPolicy {
    pub fn with_limits_ceiling(limits: ResourceLimits) -> Result<Self, SpecError> {
        validate_limits(&limits, &ResourceLimits::ceilings())?;
        Ok(Self {
            limits_ceiling: limits,
        })
    }
}

/// An immutable validated request, not proof that the environment is safe.
#[derive(Debug, Clone)]
pub struct ValidatedSpec(ExecutionSpec);

impl ValidatedSpec {
    pub fn spec(&self) -> &ExecutionSpec {
        &self.0
    }

    /// Apply only to a cleared environment after entering the private root.
    /// Callers must not merge host variables. Requests cannot override these
    /// image-local PATH/TMPDIR defaults through their environment whitelist.
    pub fn execution_environment(&self) -> BTreeMap<String, String> {
        let mut env = BTreeMap::from([
            ("LANG".into(), "C".into()),
            ("TZ".into(), "UTC".into()),
            ("TMPDIR".into(), "/tmp".into()),
        ]);
        env.extend(self.0.env.clone());
        if let Some(path) = self.0.profile.compiler_search_path() {
            env.insert("PATH".into(), path.into());
        }
        env
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationCode {
    InvalidIdentifier,
    InvalidArgv,
    UnsupportedExecutable,
    InvalidEnvironment,
    UnsafePath,
    ConflictingPath,
    TooManyArtifacts,
    InvalidLimit,
    LimitExceedsPolicy,
    InconsistentLimits,
}

/// Reports the field, never untrusted values or source contents.
#[derive(Debug, PartialEq, Eq, Serialize)]
pub struct SpecError {
    pub field: &'static str,
    pub code: ValidationCode,
}

impl fmt::Display for SpecError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {:?}", self.field, self.code)
    }
}

impl std::error::Error for SpecError {}

fn error(field: &'static str, code: ValidationCode) -> SpecError {
    SpecError { field, code }
}

fn identifier(value: &str, field: &'static str) -> Result<(), SpecError> {
    if value.is_empty()
        || value.len() > 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err(error(field, ValidationCode::InvalidIdentifier));
    }
    Ok(())
}

pub(crate) fn path(value: &str, field: &'static str) -> Result<(), SpecError> {
    if value.is_empty()
        || value.len() > MAX_PATH_BYTES
        || value.split('/').any(|part| {
            part.is_empty()
                || part == "."
                || part == ".."
                || part.len() > 255
                || !part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
        })
    {
        return Err(error(field, ValidationCode::UnsafePath));
    }
    Ok(())
}

fn overlaps(left: &str, right: &str) -> bool {
    left == right
        || left
            .strip_prefix(right)
            .is_some_and(|tail| tail.starts_with('/'))
        || right
            .strip_prefix(left)
            .is_some_and(|tail| tail.starts_with('/'))
}

fn validate_limits(limits: &ResourceLimits, cap: &ResourceLimits) -> Result<(), SpecError> {
    let bounds = [
        ("cpu_time_us", limits.cpu_time_us, 1000, cap.cpu_time_us),
        ("wall_time_us", limits.wall_time_us, 1000, cap.wall_time_us),
        ("memory_bytes", limits.memory_bytes, 1, cap.memory_bytes),
        ("stack_bytes", limits.stack_bytes, 1, cap.stack_bytes),
        (
            "processes",
            u64::from(limits.processes),
            1,
            u64::from(cap.processes),
        ),
        (
            "open_files",
            u64::from(limits.open_files),
            3,
            u64::from(cap.open_files),
        ),
        ("stdout_bytes", limits.stdout_bytes, 1, cap.stdout_bytes),
        ("stderr_bytes", limits.stderr_bytes, 1, cap.stderr_bytes),
        ("file_bytes", limits.file_bytes, 1, cap.file_bytes),
        ("workdir_bytes", limits.workdir_bytes, 1, cap.workdir_bytes),
    ];
    for (field, value, minimum, maximum) in bounds {
        if value < minimum {
            return Err(error(field, ValidationCode::InvalidLimit));
        }
        if value > maximum {
            return Err(error(field, ValidationCode::LimitExceedsPolicy));
        }
    }
    if limits.stack_bytes > limits.memory_bytes || limits.file_bytes > limits.workdir_bytes {
        return Err(error("limits", ValidationCode::InconsistentLimits));
    }
    Ok(())
}

impl ExecutionSpec {
    pub fn validate(self, policy: &SpecPolicy) -> Result<ValidatedSpec, SpecError> {
        identifier(&self.run_id, "run_id")?;
        if self.argv.is_empty()
            || self.argv.len() > 128
            || self
                .argv
                .iter()
                .any(|arg| arg.len() > 4096 || arg.contains('\0'))
            || self.argv.iter().map(String::len).sum::<usize>() > 32_768
        {
            return Err(error("argv", ValidationCode::InvalidArgv));
        }
        for (key, value) in &self.env {
            let allowed = match key.as_str() {
                "LANG" | "LC_ALL" => matches!(value.as_str(), "C" | "C.UTF-8"),
                "TZ" => value == "UTC",
                _ => false,
            };
            if !allowed {
                return Err(error("env", ValidationCode::InvalidEnvironment));
            }
        }
        if self.cwd != "." {
            path(&self.cwd, "cwd")?;
        }
        if self.inputs.len() > 64 || self.outputs.len() > 16 {
            return Err(error("artifacts", ValidationCode::TooManyArtifacts));
        }
        let mut input_paths = BTreeSet::new();
        for input in &self.inputs {
            identifier(&input.artifact_id, "inputs.artifact_id")?;
            path(&input.path, "inputs.path")?;
            if input_paths
                .iter()
                .any(|other: &&str| overlaps(other, &input.path))
                || self.cwd == input.path
                || self
                    .cwd
                    .strip_prefix(&input.path)
                    .is_some_and(|tail| tail.starts_with('/'))
            {
                return Err(error("inputs.path", ValidationCode::ConflictingPath));
            }
            input_paths.insert(input.path.as_str());
        }
        if let Some(stdin) = &self.stdin_artifact {
            identifier(stdin, "stdin_artifact")?;
        }
        let mut output_paths = BTreeSet::new();
        for output in &self.outputs {
            path(output, "outputs")?;
            if input_paths.iter().any(|input| overlaps(input, output))
                || output_paths
                    .iter()
                    .any(|other: &&str| overlaps(other, output))
            {
                return Err(error("outputs", ValidationCode::ConflictingPath));
            }
            output_paths.insert(output.as_str());
        }
        let executable = &self.argv[0];
        let approved = match self.profile {
            ExecutionProfile::Native => executable
                .strip_prefix("/work/")
                .is_some_and(|relative| input_paths.contains(relative)),
            ExecutionProfile::CompileC => executable == "/usr/bin/gcc",
            ExecutionProfile::CompileCpp => executable == "/usr/bin/g++",
        };
        if !approved {
            return Err(error("argv[0]", ValidationCode::UnsupportedExecutable));
        }
        validate_limits(&self.limits, &policy.limits_ceiling)?;
        Ok(ValidatedSpec(self))
    }
}
