//! Explicit multi-runtime protocol. Version 1 remains an independent wire contract.
use super::judging as v1;
use serde::{Deserialize, Serialize, de::Error};
pub use v1::{
    ArtifactReference, CaseReport, Comparison, CompilationStatus, CompilerDiagnostics, Digest,
    FailureCode, Identifier, Label, LeaseBinding, LeaseToken, ManifestKind, ResultKind, RunLimits,
    Stage, StageKind, StoppedKind, TestCase, Validation, Verdict,
};
use v1::{bounded, validated_struct};

pub const MAX_CLASS_FILES: usize = 256;
pub const JVM_NATIVE_RESERVE_BYTES: u64 = 160 * v1::MIB;
pub const JVM_THREAD_STACK_BYTES: u64 = 256 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u16", into = "u16")]
pub struct SchemaVersion;
impl TryFrom<u16> for SchemaVersion {
    type Error = &'static str;
    fn try_from(value: u16) -> Result<Self, Self::Error> {
        if value == 2 {
            Ok(Self)
        } else {
            Err("unsupported multi-runtime version")
        }
    }
}
impl From<SchemaVersion> for u16 {
    fn from(_: SchemaVersion) -> Self {
        2
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Language {
    #[serde(rename = "c17")]
    C17,
    #[serde(rename = "cpp20")]
    Cpp20,
    #[serde(rename = "python313")]
    Python313,
    #[serde(rename = "java21")]
    Java21,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ImageKind {
    #[serde(rename = "linux_gnu_cxx_x86_64_v1")]
    GnuCxx,
    #[serde(rename = "linux_cpython313_x86_64_v1")]
    CPython313,
    #[serde(rename = "linux_openjdk21_x86_64_v1")]
    OpenJdk21,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PreparationKind {
    Compile,
    SyntaxCheck,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProgramKind {
    NativeExecutable,
    PythonSource,
    JavaClasses,
}

pub struct RuntimeContract {
    pub source_name: &'static str,
    pub preparation_profile: &'static str,
    pub run_profile: &'static str,
    pub program_kind: ProgramKind,
    pub preparation_kind: PreparationKind,
}
impl Language {
    pub fn contract(self) -> RuntimeContract {
        let (source_name, preparation_profile, run_profile, program_kind, preparation_kind) =
            match self {
                Self::C17 => (
                    "main.c",
                    "compile_c",
                    "native",
                    ProgramKind::NativeExecutable,
                    PreparationKind::Compile,
                ),
                Self::Cpp20 => (
                    "main.cpp",
                    "compile_cpp",
                    "native",
                    ProgramKind::NativeExecutable,
                    PreparationKind::Compile,
                ),
                Self::Python313 => (
                    "main.py",
                    "python_check",
                    "python_run",
                    ProgramKind::PythonSource,
                    PreparationKind::SyntaxCheck,
                ),
                Self::Java21 => (
                    "Main.java",
                    "java_compile",
                    "java_run",
                    ProgramKind::JavaClasses,
                    PreparationKind::Compile,
                ),
            };
        RuntimeContract {
            source_name,
            preparation_profile,
            run_profile,
            program_kind,
            preparation_kind,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String")]
pub struct RuntimeVersion(String);
impl TryFrom<String> for RuntimeVersion {
    type Error = &'static str;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        if value.is_empty()
            || value.len() > 64
            || !value.as_bytes()[0].is_ascii_alphanumeric()
            || !value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.+-".contains(&b))
        {
            return Err("invalid runtime build version");
        }
        Ok(Self(value))
    }
}
impl RuntimeVersion {
    pub fn as_str(&self) -> &str {
        &self.0
    }
    fn is_java21_build(&self) -> bool {
        let value = self.0.strip_suffix("-LTS").unwrap_or(&self.0);
        let Some((version, build)) = value.split_once('+') else {
            return false;
        };
        let parts: Vec<_> = version.split('.').collect();
        (parts.len() == 3 || parts.len() == 4)
            && parts[0] == "21"
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
            && !build.is_empty()
            && build.as_bytes()[0] != b'0'
            && build.bytes().all(|b| b.is_ascii_digit())
    }
}
validated_struct!(RuntimeIdentity {
    toolchain_id: Label,
    image_kind: ImageKind,
    image_revision: u32,
    image_digest: Digest,
    runtime_version: RuntimeVersion,
});
impl RuntimeIdentity {
    pub fn validate(&self) -> Validation {
        bounded(u64::from(self.image_revision), 1, u64::from(u32::MAX))
    }
    pub fn validate_for_language(&self, language: Language) -> Validation {
        self.validate()?;
        let (id, kind) = match language {
            Language::C17 | Language::Cpp20 => ("gnu-cxx-v1", ImageKind::GnuCxx),
            Language::Python313 => ("cpython313-v1", ImageKind::CPython313),
            Language::Java21 => ("openjdk21-v1", ImageKind::OpenJdk21),
        };
        if self.toolchain_id.as_str() != id || self.image_kind != kind {
            return Err("runtime family differs from language contract");
        }
        if language == Language::Python313 && self.runtime_version.as_str() != "3.13.16" {
            return Err("unsupported CPython version");
        }
        if language == Language::Java21 && !self.runtime_version.is_java21_build() {
            return Err("JDK version must identify a fixed Java 21 build");
        }
        Ok(())
    }
}

pub fn jvm_heap_bytes(limits: &RunLimits) -> Result<u64, &'static str> {
    let reserve = JVM_NATIVE_RESERVE_BYTES + u64::from(limits.processes) * JVM_THREAD_STACK_BYTES;
    let heap = limits.memory_bytes.saturating_sub(reserve) / v1::MIB * v1::MIB;
    if heap < 32 * v1::MIB || limits.stack_bytes < JVM_THREAD_STACK_BYTES {
        return Err("budget cannot cover JVM heap and explicit non-heap reserve");
    }
    Ok(heap)
}
pub fn validate_startup_budget(
    language: Language,
    limits: &RunLimits,
    preparing: bool,
) -> Validation {
    limits.validate()?;
    let floor = match (language, preparing) {
        (Language::Python313, _) => (10_000, 50_000, 32 * v1::MIB, 1, 16),
        (Language::Java21, true) => (1_000_000, 3_000_000, 384 * v1::MIB, 16, 32),
        (Language::Java21, false) => (200_000, 1_000_000, 256 * v1::MIB, 16, 32),
        _ => return Ok(()),
    };
    if limits.cpu_time_us < floor.0
        || limits.wall_time_us < floor.1
        || limits.memory_bytes < floor.2
        || limits.processes < floor.3
        || limits.open_files < floor.4
    {
        return Err("budget below runtime startup policy");
    }
    if language == Language::Java21 {
        jvm_heap_bytes(limits)?;
    }
    Ok(())
}
validated_struct!(LanguageConfiguration {
    language: Language,
    toolchain: RuntimeIdentity,
    compile_limits: RunLimits,
    run_limits: RunLimits,
    job_wall_time_us: u64,
});
impl LanguageConfiguration {
    pub fn validate(&self) -> Validation {
        self.toolchain.validate_for_language(self.language)?;
        validate_startup_budget(self.language, &self.compile_limits, true)?;
        validate_startup_budget(self.language, &self.run_limits, false)?;
        bounded(self.job_wall_time_us, 1000, v1::MAX_JOB_WALL_US)?;
        if self.compile_limits.wall_time_us + self.run_limits.wall_time_us > self.job_wall_time_us {
            return Err("job deadline cannot cover preparation and execution");
        }
        Ok(())
    }
}
validated_struct!(JobManifest {
    schema_version: SchemaVersion, kind: ManifestKind, evaluation_id: Identifier,
    submission_id: Identifier, problem_version_id: Identifier, language: Language,
    toolchain: RuntimeIdentity, source: ArtifactReference, cases: Vec<TestCase>,
    comparison: Comparison, compile_limits: RunLimits, run_limits: RunLimits, job_wall_time_us: u64,
});

validated_struct!(ProgramFile {
    path: String,
    artifact: ArtifactReference
});
impl ProgramFile {
    pub fn validate(&self) -> Validation {
        self.artifact.validate()?;
        if self.path.is_empty() || self.path.len() > 128 {
            return Err("invalid program file path");
        }
        Ok(())
    }
}
validated_struct!(CompiledProgram {
    kind: ProgramKind, binding: LeaseBinding, source: ArtifactReference,
    toolchain: RuntimeIdentity, files: Vec<ProgramFile>,
});
impl CompiledProgram {
    pub fn bytes(&self) -> u64 {
        self.files.iter().map(|f| f.artifact.bytes).sum()
    }
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        self.source.validate()?;
        self.toolchain.validate()?;
        bounded(self.source.bytes, 1, v1::MAX_SOURCE_BYTES)?;
        if self.files.is_empty() || self.files.len() > MAX_CLASS_FILES {
            return Err("program file count outside policy");
        }
        let mut last: Option<&str> = None;
        for file in &self.files {
            file.validate()?;
            if file.artifact.bytes == 0 || last.is_some_and(|v| v >= file.path.as_str()) {
                return Err("program files empty, duplicated or not in canonical order");
            }
            last = Some(&file.path);
        }
        if self.bytes() > v1::MAX_PACKAGE_BYTES {
            return Err("program byte count outside policy");
        }
        match self.kind {
            ProgramKind::NativeExecutable => {
                self.toolchain.validate_for_language(Language::C17)?;
                if self.files.len() != 1 || self.files[0].path != "main" {
                    return Err("invalid native program");
                }
            }
            ProgramKind::PythonSource => {
                self.toolchain.validate_for_language(Language::Python313)?;
                if self.files.len() != 1
                    || self.files[0].path != "main.py"
                    || self.files[0].artifact != self.source
                {
                    return Err("Python program must be the exact checked source");
                }
            }
            ProgramKind::JavaClasses => {
                self.toolchain.validate_for_language(Language::Java21)?;
                if !self.files.iter().any(|f| f.path == "Main.class")
                    || self.files.iter().any(|f| !class_name(&f.path))
                {
                    return Err("Java requires a bounded default-package Main class set");
                }
            }
        }
        Ok(())
    }
}
pub fn class_name(path: &str) -> bool {
    let Some(stem) = path.strip_suffix(".class") else {
        return false;
    };
    !stem.is_empty()
        && (stem.as_bytes()[0].is_ascii_alphabetic() || b"_$".contains(&stem.as_bytes()[0]))
        && stem
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_$".contains(&b))
}
validated_struct!(CompilationReport {
    status: CompilationStatus,
    #[serde(deserialize_with = "v1::required_option")] program: Option<CompiledProgram>,
    diagnostics: CompilerDiagnostics, diagnostics_truncated: bool,
    #[serde(deserialize_with = "v1::required_option")] cpu_time_us: Option<u64>,
    #[serde(deserialize_with = "v1::required_option")] memory_peak_bytes: Option<u64>,
});
impl CompilationReport {
    pub fn validate(&self) -> Validation {
        for value in [self.cpu_time_us, self.memory_peak_bytes]
            .into_iter()
            .flatten()
        {
            bounded(value, 0, v1::I64_MAX)?;
        }
        if (self.status == CompilationStatus::Succeeded) != self.program.is_some() {
            return Err("compiled artifact only belongs to successful compilation");
        }
        if let Some(program) = &self.program {
            program.validate()?;
        }
        Ok(())
    }
}
validated_struct!(JudgementReport {
    schema_version: SchemaVersion, kind: ResultKind, binding: LeaseBinding, manifest_digest: Digest,
    compilation: CompilationReport, cases: Vec<CaseReport>, verdict: Verdict, cleanup_complete: bool,
    #[serde(deserialize_with = "v1::required_option")] failure_code: Option<FailureCode>,
    #[serde(deserialize_with = "v1::required_option")] run_cpu_time_us: Option<u64>,
    #[serde(deserialize_with = "v1::required_option")] run_memory_peak_bytes: Option<u64>,
});
validated_struct!(LeaseGrant {
    schema_version: SchemaVersion,
    kind: v1::LeaseKind,
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
        bounded(self.expires_at_unix_ms, 1, v1::I64_MAX)
    }
}
validated_struct!(StageReport {
    schema_version: SchemaVersion, kind: StageKind, binding: LeaseBinding, sequence: u64,
    stage: Stage, preparation_kind: PreparationKind,
    #[serde(deserialize_with = "v1::required_option")] case_index: Option<u16>,
});
impl StageReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        bounded(self.sequence, 1, v1::I64_MAX)?;
        if (self.stage == Stage::Running) != self.case_index.is_some()
            || self
                .case_index
                .is_some_and(|i| usize::from(i) >= v1::MAX_CASES)
        {
            return Err("testcase index only belongs to running phase");
        }
        Ok(())
    }
    pub fn validate_for_manifest(&self, manifest: &JobManifest) -> Validation {
        self.validate()?;
        manifest.validate()?;
        if self.binding.evaluation_id != manifest.evaluation_id
            || self.preparation_kind != manifest.language.contract().preparation_kind
        {
            return Err("stage differs from authorized language preparation");
        }
        Ok(())
    }
}
validated_struct!(StoppedReport {
    schema_version: SchemaVersion,
    kind: StoppedKind,
    binding: LeaseBinding,
    cleanup_complete: bool
});
impl StoppedReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()
    }
}
validated_struct!(RuntimeCapability {
    language: Language,
    toolchain: RuntimeIdentity,
    compile_ceiling: RunLimits,
    run_ceiling: RunLimits
});
impl RuntimeCapability {
    pub fn validate(&self) -> Validation {
        self.toolchain.validate_for_language(self.language)?;
        validate_startup_budget(self.language, &self.compile_ceiling, true)?;
        validate_startup_budget(self.language, &self.run_ceiling, false)
    }
}
validated_struct!(AdmissionPolicy {
    schema_version: SchemaVersion,
    #[serde(default = "one")] max_concurrent_jobs: u32,
    #[serde(default = "one")] prefetch: u32,
    #[serde(default = "two")] max_retries: u32,
    max_job_wall_time_us: u64, max_package_bytes: u64, capabilities: Vec<RuntimeCapability>,
});
fn one() -> u32 {
    1
}
fn two() -> u32 {
    2
}
impl AdmissionPolicy {
    pub fn validate(&self) -> Validation {
        bounded(u64::from(self.max_concurrent_jobs), 1, 64)?;
        bounded(u64::from(self.prefetch), 1, 64)?;
        bounded(u64::from(self.max_retries), 0, 4)?;
        bounded(self.max_job_wall_time_us, 1000, v1::MAX_JOB_WALL_US)?;
        bounded(self.max_package_bytes, 1, v1::MAX_PACKAGE_BYTES)?;
        if self.prefetch > self.max_concurrent_jobs
            || self.capabilities.is_empty()
            || self.capabilities.len() > 4
        {
            return Err("runtime admission capacity outside policy");
        }
        let mut seen = std::collections::HashSet::new();
        for cap in &self.capabilities {
            cap.validate()?;
            if !seen.insert(cap.language) {
                return Err("duplicate language capability");
            }
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
        let cap = self
            .capabilities
            .iter()
            .find(|c| c.language == manifest.language)
            .ok_or("unsupported runtime capability")?;
        if cap.toolchain != manifest.toolchain
            || !manifest.compile_limits.fits(&cap.compile_ceiling)
            || !manifest.run_limits.fits(&cap.run_ceiling)
            || manifest.job_wall_time_us > self.max_job_wall_time_us
            || manifest.package_bytes() > self.max_package_bytes
        {
            return Err("job exceeds trusted runtime capability or budget");
        }
        Ok(())
    }
}

fn measure(value: Option<u64>) -> Validation {
    value.map_or(Ok(()), |v| bounded(v, 0, v1::I64_MAX))
}

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
        self.toolchain.validate_for_language(self.language)?;
        validate_startup_budget(self.language, &self.compile_limits, true)?;
        validate_startup_budget(self.language, &self.run_limits, false)?;
        self.compile_limits.validate()?;
        self.run_limits.validate()?;
        bounded(self.source.bytes, 1, v1::MAX_SOURCE_BYTES)?;
        bounded(self.job_wall_time_us, 1000, v1::MAX_JOB_WALL_US)?;
        if self.cases.is_empty() || self.cases.len() > v1::MAX_CASES {
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
        if self.package_bytes() > v1::MAX_PACKAGE_BYTES {
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

impl JudgementReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        self.compilation.validate()?;
        if self
            .compilation
            .program
            .as_ref()
            .is_some_and(|p| p.binding != self.binding)
        {
            return Err("program belongs to another attempt");
        }
        measure(self.run_cpu_time_us)?;
        measure(self.run_memory_peak_bytes)?;
        if self.cases.len() > v1::MAX_CASES {
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
        if self.compilation.program.as_ref().is_some_and(|p| {
            p.source != manifest.source
                || p.toolchain != manifest.toolchain
                || p.kind != manifest.language.contract().program_kind
        }) {
            return Err("program differs from authorized source or runtime");
        }
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

validated_struct!(AdmissionRegistry {
    schema_version: SchemaVersion,
    #[serde(deserialize_with = "v1::required_option")] legacy: Option<v1::AdmissionPolicy>,
    #[serde(deserialize_with = "v1::required_option")] multi_runtime: Option<AdmissionPolicy>,
});
impl AdmissionRegistry {
    pub fn validate(&self) -> Validation {
        if self.legacy.is_none() && self.multi_runtime.is_none() {
            return Err("node must advertise at least one explicit protocol capability");
        }
        if let Some(policy) = &self.legacy {
            policy.validate()?;
        }
        if let Some(policy) = &self.multi_runtime {
            policy.validate()?;
        }
        if let (Some(legacy), Some(multi)) = (&self.legacy, &self.multi_runtime)
            && (legacy.max_concurrent_jobs != multi.max_concurrent_jobs
                || legacy.prefetch != multi.prefetch
                || legacy.max_retries != multi.max_retries)
        {
            return Err("protocol policies disagree on node capacity or retries");
        }
        Ok(())
    }
    pub fn admit(
        &self,
        manifest: &super::judging_versions::JobManifest,
        attempt_number: u32,
    ) -> Validation {
        self.validate()?;
        match manifest {
            super::judging_versions::JobManifest::V1(manifest) => self
                .legacy
                .as_ref()
                .ok_or("unsupported manifest protocol")?
                .admit(manifest, attempt_number),
            super::judging_versions::JobManifest::V2(manifest) => self
                .multi_runtime
                .as_ref()
                .ok_or("unsupported manifest protocol")?
                .admit(manifest, attempt_number),
        }
    }
}

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
