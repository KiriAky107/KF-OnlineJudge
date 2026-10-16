//! Validated judge wire values. No execution, authorization service or storage.
use crate::SchemaVersion;
use serde::{Deserialize, Serialize, de::Error};
use uuid::Uuid;

pub const MIB: u64 = 1024 * 1024;
pub const I64_MAX: u64 = i64::MAX as u64;
pub const MAX_SOURCE_BYTES: u64 = 256 * 1024;
pub const MAX_PACKAGE_BYTES: u64 = 64 * MIB;
pub const MAX_CASES: usize = 256;
pub const MAX_JOB_WALL_US: u64 = 3_600_000_000;
pub const MAX_WIRE_BYTES: usize = 256 * 1024;
pub type Validation = Result<(), &'static str>;

/// Bound before parsing; hide serde diagnostics because they can contain input values.
pub fn decode_wire<T: serde::de::DeserializeOwned>(payload: &[u8]) -> Result<T, &'static str> {
    if payload.len() > MAX_WIRE_BYTES {
        return Err("wire payload exceeds byte budget");
    }
    serde_json::from_slice(payload).map_err(|_| "invalid wire payload")
}

/// Canonical snapshot bytes: UTF-8 JSON, recursively sorted object keys, no extra whitespace.
pub fn manifest_bytes(manifest: &JobManifest) -> Result<Vec<u8>, &'static str> {
    manifest.validate()?;
    let mut value = serde_json::to_value(manifest).map_err(|_| "invalid manifest")?;
    fn sorted(value: &mut serde_json::Value) {
        match value {
            serde_json::Value::Object(map) => {
                let mut entries: Vec<_> = std::mem::take(map).into_iter().collect();
                entries.sort_by(|a, b| a.0.cmp(&b.0));
                for (_, nested) in &mut entries {
                    sorted(nested);
                }
                *map = entries.into_iter().collect();
            }
            serde_json::Value::Array(values) => values.iter_mut().for_each(sorted),
            _ => {}
        }
    }
    sorted(&mut value);
    serde_json::to_vec(&value).map_err(|_| "invalid manifest")
}

pub(crate) fn bounded(value: u64, min: u64, max: u64) -> Validation {
    if (min..=max).contains(&value) {
        Ok(())
    } else {
        Err("value outside policy")
    }
}
fn measure(value: Option<u64>) -> Validation {
    value.map_or(Ok(()), |v| bounded(v, 0, I64_MAX))
}
pub(crate) fn required_option<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

// All decoded nested values validate before becoming public wire models.
macro_rules! validated_struct {
    ($name:ident { $($(#[$attr:meta])* $field:ident: $ty:ty),* $(,)? }) => {
        #[derive(Clone, Debug, PartialEq, Eq, Serialize)]
        pub struct $name { $($(#[$attr])* pub $field: $ty),* }
        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                #[derive(Deserialize)]
                #[serde(deny_unknown_fields)]
                struct Wire { $($(#[$attr])* $field: $ty),* }
                let raw = Wire::deserialize(d)?;
                let value = Self { $($field: raw.$field),* };
                value.validate().map_err(D::Error::custom)?;
                Ok(value)
            }
        }
    };
}
pub(crate) use validated_struct;
macro_rules! kind {
    ($name:ident, $wire:literal) => {
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
        pub enum $name {
            #[serde(rename = $wire)]
            Value,
        }
    };
}
kind!(ManifestKind, "judge.manifest");
kind!(LeaseKind, "judge.lease");
kind!(StageKind, "judge.stage");
kind!(ResultKind, "judge.result");
kind!(StoppedKind, "judge.stopped");
kind!(ImageKind, "linux_gnu_cxx_x86_64_v1");
kind!(Comparison, "exact_bytes");

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "String")]
pub struct Identifier(Uuid);
impl TryFrom<String> for Identifier {
    type Error = &'static str;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        let id = Uuid::parse_str(&value).map_err(|_| "invalid identifier")?;
        if id.is_nil() || id.to_string() != value {
            return Err("identifier must be non-nil and canonical");
        }
        Ok(Self(id))
    }
}
impl Identifier {
    pub fn as_uuid(self) -> Uuid {
        self.0
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String")]
pub struct Digest(String);
impl TryFrom<String> for Digest {
    type Error = &'static str;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        if value.len() != 64
            || !value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err("invalid digest");
        }
        Ok(Self(value))
    }
}
impl Digest {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct LeaseToken(Digest);
impl std::fmt::Debug for LeaseToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LeaseToken([redacted])")
    }
}
impl LeaseToken {
    pub fn expose_secret(&self) -> &str {
        self.0.as_str()
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String")]
pub struct CompilerDiagnostics(String);
impl TryFrom<String> for CompilerDiagnostics {
    type Error = &'static str;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        if value.len() > 65536 {
            return Err("compiler diagnostics exceed byte budget");
        }
        Ok(Self(value))
    }
}
impl std::fmt::Debug for CompilerDiagnostics {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("CompilerDiagnostics([redacted])")
    }
}
impl CompilerDiagnostics {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String")]
pub struct Label(String);
impl TryFrom<String> for Label {
    type Error = &'static str;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        let first = value
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit());
        if !first
            || value.len() > 64
            || !value
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
        {
            return Err("invalid label");
        }
        Ok(Self(value))
    }
}
impl Label {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Language {
    C17,
    Cpp20,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Accepted,
    WrongAnswer,
    CompilationError,
    RuntimeError,
    TimeLimitExceeded,
    MemoryLimitExceeded,
    OutputLimitExceeded,
    Cancelled,
    SystemError,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompilationStatus {
    Succeeded,
    CompilationError,
    CompilationLimit,
    Cancelled,
    SystemError,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureCode {
    ArtifactUnavailable,
    SandboxUnavailable,
    CleanupFailed,
    LeaseLost,
    JobDeadline,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Compiling,
    Running,
}

validated_struct!(ArtifactReference {
    digest: Digest,
    bytes: u64
});
impl ArtifactReference {
    pub fn validate(&self) -> Validation {
        bounded(self.bytes, 0, MAX_PACKAGE_BYTES)
    }
}
validated_struct!(Toolchain {
    toolchain_id: Label,
    image_kind: ImageKind,
    image_revision: u32
});
impl Toolchain {
    pub fn validate(&self) -> Validation {
        bounded(u64::from(self.image_revision), 1, u64::from(u32::MAX))
    }
}
validated_struct!(RunLimits {
    cpu_time_us: u64,
    wall_time_us: u64,
    memory_bytes: u64,
    stack_bytes: u64,
    processes: u32,
    open_files: u32,
    stdout_bytes: u64,
    stderr_bytes: u64,
    file_bytes: u64,
    workdir_bytes: u64,
});
impl RunLimits {
    pub fn validate(&self) -> Validation {
        for (value, min, max) in [
            (self.cpu_time_us, 1000, 60_000_000),
            (self.wall_time_us, 1000, 120_000_000),
            (self.memory_bytes, 1, 2048 * MIB),
            (self.stack_bytes, 1, 256 * MIB),
            (u64::from(self.processes), 1, 128),
            (u64::from(self.open_files), 3, 1024),
            (self.stdout_bytes, 1, 16 * MIB),
            (self.stderr_bytes, 1, 16 * MIB),
            (self.file_bytes, 1, 64 * MIB),
            (self.workdir_bytes, 1, 256 * MIB),
        ] {
            bounded(value, min, max)?;
        }
        if self.stack_bytes > self.memory_bytes
            || self.file_bytes > self.workdir_bytes
            || self.stdout_bytes > self.file_bytes
            || self.stderr_bytes > self.file_bytes
            || self.stdout_bytes + self.stderr_bytes > self.workdir_bytes
        {
            return Err("inconsistent resource limits");
        }
        Ok(())
    }
    pub fn fits(&self, ceiling: &Self) -> bool {
        self.cpu_time_us <= ceiling.cpu_time_us
            && self.wall_time_us <= ceiling.wall_time_us
            && self.memory_bytes <= ceiling.memory_bytes
            && self.stack_bytes <= ceiling.stack_bytes
            && self.processes <= ceiling.processes
            && self.open_files <= ceiling.open_files
            && self.stdout_bytes <= ceiling.stdout_bytes
            && self.stderr_bytes <= ceiling.stderr_bytes
            && self.file_bytes <= ceiling.file_bytes
            && self.workdir_bytes <= ceiling.workdir_bytes
    }
}
validated_struct!(TestCase {
    case_id: Identifier,
    input: ArtifactReference,
    expected: ArtifactReference
});
impl TestCase {
    pub fn validate(&self) -> Validation {
        self.input.validate()?;
        self.expected.validate()
    }
}
validated_struct!(JobManifest {
    schema_version: SchemaVersion, kind: ManifestKind, evaluation_id: Identifier,
    submission_id: Identifier, problem_version_id: Identifier, language: Language,
    toolchain: Toolchain, source: ArtifactReference, cases: Vec<TestCase>,
    comparison: Comparison, compile_limits: RunLimits, run_limits: RunLimits, job_wall_time_us: u64,
});
impl JobManifest {
    pub fn package_bytes(&self) -> u64 {
        self.source.bytes
            + self
                .cases
                .iter()
                .map(|c| c.input.bytes + c.expected.bytes)
                .sum::<u64>()
    }
    pub fn validate(&self) -> Validation {
        self.source.validate()?;
        self.toolchain.validate()?;
        self.compile_limits.validate()?;
        self.run_limits.validate()?;
        bounded(self.source.bytes, 1, MAX_SOURCE_BYTES)?;
        bounded(self.job_wall_time_us, 1000, MAX_JOB_WALL_US)?;
        if self.cases.is_empty() || self.cases.len() > MAX_CASES {
            return Err("testcase count outside policy");
        }
        let mut seen = std::collections::HashSet::new();
        for c in &self.cases {
            c.validate()?;
            if !seen.insert(c.case_id) {
                return Err("duplicate testcase identifiers");
            }
            if c.expected.bytes > self.run_limits.stdout_bytes {
                return Err("expected output exceeds output budget");
            }
        }
        if self.package_bytes() > MAX_PACKAGE_BYTES {
            return Err("test package outside policy");
        }
        let required = self.compile_limits.wall_time_us
            + self.cases.len() as u64 * self.run_limits.wall_time_us;
        if required > self.job_wall_time_us {
            return Err("job deadline cannot cover execution budgets");
        }
        Ok(())
    }
}
validated_struct!(LeaseBinding {
    evaluation_id: Identifier,
    attempt_id: Identifier,
    worker_id: Identifier,
    fencing_version: u64
});
impl LeaseBinding {
    pub fn validate(&self) -> Validation {
        bounded(self.fencing_version, 1, I64_MAX)
    }
}
validated_struct!(LeaseGrant {
    schema_version: SchemaVersion,
    kind: LeaseKind,
    binding: LeaseBinding,
    lease_token: LeaseToken,
    attempt_number: u32,
    expires_at_unix_ms: u64,
    manifest_digest: Digest,
});
impl LeaseGrant {
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        bounded(u64::from(self.attempt_number), 1, 5)?;
        bounded(self.expires_at_unix_ms, 1, I64_MAX)
    }
}
validated_struct!(StageReport {
    schema_version: SchemaVersion, kind: StageKind, binding: LeaseBinding, sequence: u64,
    stage: Stage,
    #[serde(deserialize_with = "required_option")] case_index: Option<u16>,
});
impl StageReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        bounded(self.sequence, 1, I64_MAX)?;
        if (self.stage == Stage::Running) != self.case_index.is_some()
            || self.case_index.is_some_and(|i| usize::from(i) >= MAX_CASES)
        {
            return Err("testcase index only belongs to running phase");
        }
        Ok(())
    }
}
// A trusted ordinary Worker attests actual Root stop/recovery proof. No execution
// result is accepted through this path; current attempt/fence still bind it.
validated_struct!(StoppedReport {
    schema_version: SchemaVersion,
    kind: StoppedKind,
    binding: LeaseBinding,
    cleanup_complete: bool,
});
impl StoppedReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()
    }
}
validated_struct!(CompilationReport {
    status: CompilationStatus,
    #[serde(deserialize_with = "required_option")] program: Option<ArtifactReference>,
    diagnostics: CompilerDiagnostics, diagnostics_truncated: bool,
    #[serde(deserialize_with = "required_option")] cpu_time_us: Option<u64>,
    #[serde(deserialize_with = "required_option")] memory_peak_bytes: Option<u64>,
});
impl CompilationReport {
    pub fn validate(&self) -> Validation {
        measure(self.cpu_time_us)?;
        measure(self.memory_peak_bytes)?;
        if self.diagnostics.as_str().len() > 65536 {
            return Err("compiler diagnostics exceed byte budget");
        }
        if (self.status == CompilationStatus::Succeeded) != self.program.is_some() {
            return Err("compiled artifact only belongs to successful compilation");
        }
        if let Some(program) = &self.program {
            program.validate()?;
            bounded(program.bytes, 1, MAX_PACKAGE_BYTES)?;
        }
        Ok(())
    }
}
validated_struct!(CaseReport {
    case_id: Identifier, verdict: Verdict,
    #[serde(deserialize_with = "required_option")] cpu_time_us: Option<u64>,
    #[serde(deserialize_with = "required_option")] memory_peak_bytes: Option<u64>,
});
impl CaseReport {
    pub fn validate(&self) -> Validation {
        measure(self.cpu_time_us)?;
        measure(self.memory_peak_bytes)?;
        if self.verdict == Verdict::CompilationError {
            return Err("compilation failure is not a testcase outcome");
        }
        Ok(())
    }
}
validated_struct!(JudgementReport {
    schema_version: SchemaVersion, kind: ResultKind, binding: LeaseBinding, manifest_digest: Digest,
    compilation: CompilationReport, cases: Vec<CaseReport>, verdict: Verdict, cleanup_complete: bool,
    #[serde(deserialize_with = "required_option")] failure_code: Option<FailureCode>,
    #[serde(deserialize_with = "required_option")] run_cpu_time_us: Option<u64>,
    #[serde(deserialize_with = "required_option")] run_memory_peak_bytes: Option<u64>,
});
impl JudgementReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        self.compilation.validate()?;
        measure(self.run_cpu_time_us)?;
        measure(self.run_memory_peak_bytes)?;
        if self.cases.len() > MAX_CASES {
            return Err("testcase count outside policy");
        }
        let mut seen = std::collections::HashSet::new();
        for (i, c) in self.cases.iter().enumerate() {
            c.validate()?;
            if !seen.insert(c.case_id) {
                return Err("duplicate testcase results");
            }
            if matches!(c.verdict, Verdict::SystemError | Verdict::Cancelled)
                && i != self.cases.len() - 1
            {
                return Err("results after fatal testcase outcome");
            }
        }
        if !self.cleanup_complete && self.failure_code != Some(FailureCode::CleanupFailed) {
            return Err("incomplete cleanup requires its own failure evidence");
        }
        if self.cleanup_complete && self.failure_code == Some(FailureCode::CleanupFailed) {
            return Err("cleanup failure cannot claim completed cleanup");
        }
        if (self.verdict == Verdict::SystemError) != self.failure_code.is_some() {
            return Err("infrastructure failure and verdict must agree");
        }
        if self.compilation.status != CompilationStatus::Succeeded {
            let mapped = match self.compilation.status {
                CompilationStatus::CompilationError | CompilationStatus::CompilationLimit => {
                    Verdict::CompilationError
                }
                CompilationStatus::Cancelled => Verdict::Cancelled,
                CompilationStatus::SystemError => Verdict::SystemError,
                CompilationStatus::Succeeded => unreachable!(),
            };
            if !self.cases.is_empty() || self.verdict != mapped {
                return Err("failed compilation cannot have runtime results");
            }
        } else if self.failure_code.is_none() {
            if self.cases.iter().any(|c| c.verdict == Verdict::SystemError) {
                return Err("infrastructure failure requires failure code");
            }
            if self.cases.is_empty() && self.verdict != Verdict::Cancelled {
                return Err("missing testcase results cannot be a completed judgement");
            }
            if self.verdict != Verdict::Cancelled && !self.cases.is_empty() {
                let summary = self
                    .cases
                    .iter()
                    .find(|c| c.verdict == Verdict::Cancelled)
                    .or_else(|| self.cases.iter().find(|c| c.verdict != Verdict::Accepted))
                    .map_or(Verdict::Accepted, |c| c.verdict);
                if self.verdict != summary {
                    return Err("testcase summary disagrees");
                }
            }
        }
        let cpu = if !self.cases.is_empty() && self.cases.iter().all(|c| c.cpu_time_us.is_some()) {
            Some(
                self.cases
                    .iter()
                    .try_fold(0_u64, |total, c| {
                        total.checked_add(c.cpu_time_us.unwrap_or(0))
                    })
                    .ok_or("measurement sum overflow")?,
            )
        } else {
            None
        };
        let memory =
            if !self.cases.is_empty() && self.cases.iter().all(|c| c.memory_peak_bytes.is_some()) {
                self.cases.iter().filter_map(|c| c.memory_peak_bytes).max()
            } else {
                None
            };
        if self.run_cpu_time_us != cpu || self.run_memory_peak_bytes != memory {
            return Err("runtime measurement summary disagrees");
        }
        Ok(())
    }
    pub fn validate_for_manifest(&self, manifest: &JobManifest, digest: &Digest) -> Validation {
        self.validate()?;
        manifest.validate()?;
        if self.binding.evaluation_id != manifest.evaluation_id || &self.manifest_digest != digest {
            return Err("report does not belong to authorized manifest");
        }
        if self.cases.len() > manifest.cases.len()
            || !self
                .cases
                .iter()
                .zip(&manifest.cases)
                .all(|(a, b)| a.case_id == b.case_id)
        {
            return Err("testcase results differ from authorized order");
        }
        if !matches!(
            self.verdict,
            Verdict::CompilationError | Verdict::Cancelled | Verdict::SystemError
        ) && self.cases.len() != manifest.cases.len()
        {
            return Err("incomplete ordinary judgement");
        }
        Ok(())
    }
}
validated_struct!(AdmissionPolicy {
    #[serde(default = "one")]
    max_concurrent_jobs: u32,
    #[serde(default = "one")]
    prefetch: u32,
    #[serde(default = "two")]
    max_retries: u32,
    max_job_wall_time_us: u64,
    max_package_bytes: u64,
    toolchain: Toolchain,
    compile_ceiling: RunLimits,
    run_ceiling: RunLimits,
});
fn one() -> u32 {
    1
}
fn two() -> u32 {
    2
}
impl AdmissionPolicy {
    pub fn validate(&self) -> Validation {
        self.compile_ceiling.validate()?;
        self.run_ceiling.validate()?;
        self.toolchain.validate()?;
        bounded(u64::from(self.max_concurrent_jobs), 1, 64)?;
        bounded(u64::from(self.prefetch), 1, 64)?;
        bounded(u64::from(self.max_retries), 0, 4)?;
        bounded(self.max_job_wall_time_us, 1000, MAX_JOB_WALL_US)?;
        bounded(self.max_package_bytes, 1, MAX_PACKAGE_BYTES)?;
        if self.prefetch > self.max_concurrent_jobs {
            return Err("prefetch exceeds available job slots");
        }
        Ok(())
    }
    pub fn admit(&self, manifest: &JobManifest, attempt_number: u32) -> Validation {
        self.validate()?;
        manifest.validate()?;
        bounded(
            u64::from(attempt_number),
            1,
            u64::from(self.max_retries) + 1,
        )?;
        if manifest.toolchain != self.toolchain
            || !manifest.compile_limits.fits(&self.compile_ceiling)
            || !manifest.run_limits.fits(&self.run_ceiling)
            || manifest.job_wall_time_us > self.max_job_wall_time_us
            || manifest.package_bytes() > self.max_package_bytes
        {
            return Err("job exceeds trusted node budget");
        }
        Ok(())
    }
}
