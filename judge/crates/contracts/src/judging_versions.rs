//! Version dispatch without changing legacy canonical snapshots or result identities.
use super::{judging as v1, judging_v2 as v2};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum JobManifest {
    V1(v1::JobManifest),
    V2(v2::JobManifest),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum JudgementReport {
    V1(v1::JudgementReport),
    V2(v2::JudgementReport),
}
impl JobManifest {
    pub fn canonical_bytes(&self) -> Result<Vec<u8>, &'static str> {
        match self {
            Self::V1(manifest) => v1::manifest_bytes(manifest),
            Self::V2(manifest) => v2::manifest_bytes(manifest),
        }
    }
}
impl JudgementReport {
    pub fn validate_for_manifest(
        &self,
        manifest: &JobManifest,
        digest: &v1::Digest,
    ) -> v1::Validation {
        match (self, manifest) {
            (Self::V1(report), JobManifest::V1(manifest)) => {
                report.validate_for_manifest(manifest, digest)
            }
            (Self::V2(report), JobManifest::V2(manifest)) => {
                report.validate_for_manifest(manifest, digest)
            }
            _ => Err("report protocol differs from authorized manifest"),
        }
    }
}

/// Operator explicitly declares accepted generations; legacy policies are never promoted.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum NodeAdmission {
    Legacy(v1::AdmissionPolicy),
    Multi(v2::AdmissionPolicy),
    Registry(v2::AdmissionRegistry),
}
impl NodeAdmission {
    pub fn validate(&self) -> v1::Validation {
        match self {
            Self::Legacy(policy) => policy.validate(),
            Self::Multi(policy) => policy.validate(),
            Self::Registry(policy) => policy.validate(),
        }
    }
    pub fn admit(&self, manifest: &JobManifest, attempt_number: u32) -> v1::Validation {
        self.validate()?;
        match (self, manifest) {
            (Self::Legacy(policy), JobManifest::V1(manifest)) => {
                policy.admit(manifest, attempt_number)
            }
            (Self::Multi(policy), JobManifest::V2(manifest)) => {
                policy.admit(manifest, attempt_number)
            }
            (Self::Registry(policy), manifest) => policy.admit(manifest, attempt_number),
            _ => Err("node does not advertise this manifest protocol"),
        }
    }
    fn capacity(&self) -> (u32, u32, u32) {
        match self {
            Self::Legacy(policy) => (
                policy.max_concurrent_jobs,
                policy.prefetch,
                policy.max_retries,
            ),
            Self::Multi(policy) => (
                policy.max_concurrent_jobs,
                policy.prefetch,
                policy.max_retries,
            ),
            Self::Registry(registry) => {
                // Validation requires a nonempty registry and identical common node capacities.
                if let Some(policy) = &registry.legacy {
                    (
                        policy.max_concurrent_jobs,
                        policy.prefetch,
                        policy.max_retries,
                    )
                } else if let Some(policy) = &registry.multi_runtime {
                    (
                        policy.max_concurrent_jobs,
                        policy.prefetch,
                        policy.max_retries,
                    )
                } else {
                    (0, 0, 0)
                }
            }
        }
    }
    pub fn max_concurrent_jobs(&self) -> u32 {
        self.capacity().0
    }
    pub fn prefetch(&self) -> u32 {
        self.capacity().1
    }
    pub fn max_retries(&self) -> u32 {
        self.capacity().2
    }
}

macro_rules! versions {
    ($name:ident) => {
        #[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(untagged)]
        pub enum $name {
            V1(v1::$name),
            V2(v2::$name),
        }
    };
}
versions!(LeaseGrant);
versions!(StageReport);
versions!(StoppedReport);

impl JobManifest {
    pub fn version(&self) -> u16 {
        match self {
            Self::V1(_) => 1,
            Self::V2(_) => 2,
        }
    }
    pub fn cases(&self) -> &[v1::TestCase] {
        match self {
            Self::V1(m) => &m.cases,
            Self::V2(m) => &m.cases,
        }
    }
    pub fn compile_limits(&self) -> &v1::RunLimits {
        match self {
            Self::V1(m) => &m.compile_limits,
            Self::V2(m) => &m.compile_limits,
        }
    }
    pub fn problem_version_id(&self) -> v1::Identifier {
        match self {
            Self::V1(m) => m.problem_version_id,
            Self::V2(m) => m.problem_version_id,
        }
    }
    pub fn evaluation_id(&self) -> v1::Identifier {
        match self {
            Self::V1(m) => m.evaluation_id,
            Self::V2(m) => m.evaluation_id,
        }
    }
}
impl LeaseGrant {
    pub fn for_manifest(
        manifest: &JobManifest,
        binding: v1::LeaseBinding,
        lease_token: v1::LeaseToken,
        attempt_number: u32,
        expires_at_unix_ms: u64,
        manifest_digest: v1::Digest,
    ) -> Self {
        match manifest {
            JobManifest::V1(_) => Self::V1(v1::LeaseGrant {
                schema_version: crate::SchemaVersion,
                kind: v1::LeaseKind::Value,
                binding,
                lease_token,
                attempt_number,
                expires_at_unix_ms,
                manifest_digest,
            }),
            JobManifest::V2(_) => Self::V2(v2::LeaseGrant {
                schema_version: v2::SchemaVersion,
                kind: v1::LeaseKind::Value,
                binding,
                lease_token,
                attempt_number,
                expires_at_unix_ms,
                manifest_digest,
            }),
        }
    }
}
/// Shared state-machine projection only; serialization retains the original wire value.
pub struct StageFields<'a> {
    pub binding: &'a v1::LeaseBinding,
    pub sequence: u64,
    pub stage: v1::Stage,
    pub case_index: Option<u16>,
}
impl StageReport {
    pub fn fields(&self) -> StageFields<'_> {
        match self {
            Self::V1(r) => StageFields {
                binding: &r.binding,
                sequence: r.sequence,
                stage: r.stage,
                case_index: r.case_index,
            },
            Self::V2(r) => StageFields {
                binding: &r.binding,
                sequence: r.sequence,
                stage: r.stage,
                case_index: r.case_index,
            },
        }
    }
    pub fn validate_for_manifest(&self, manifest: &JobManifest) -> v1::Validation {
        match (self, manifest) {
            (Self::V1(r), JobManifest::V1(m)) => {
                r.validate()?;
                if r.binding.evaluation_id != m.evaluation_id {
                    return Err("stage belongs to another manifest");
                }
                Ok(())
            }
            (Self::V2(r), JobManifest::V2(m)) => r.validate_for_manifest(m),
            _ => Err("stage protocol differs from authorized manifest"),
        }
    }
}
pub struct ReportFields<'a> {
    pub binding: &'a v1::LeaseBinding,
    pub verdict: v1::Verdict,
    pub failure_code: Option<v1::FailureCode>,
    pub cleanup_complete: bool,
    pub compilation_status: v1::CompilationStatus,
    pub cases: &'a [v1::CaseReport],
}
impl JudgementReport {
    pub fn fields(&self) -> ReportFields<'_> {
        match self {
            Self::V1(r) => ReportFields {
                binding: &r.binding,
                verdict: r.verdict,
                failure_code: r.failure_code,
                cleanup_complete: r.cleanup_complete,
                compilation_status: r.compilation.status,
                cases: &r.cases,
            },
            Self::V2(r) => ReportFields {
                binding: &r.binding,
                verdict: r.verdict,
                failure_code: r.failure_code,
                cleanup_complete: r.cleanup_complete,
                compilation_status: r.compilation.status,
                cases: &r.cases,
            },
        }
    }
    pub fn validate_program_budget(&self, manifest: &JobManifest) -> v1::Validation {
        let limits = manifest.compile_limits();
        let fits = match self {
            Self::V1(r) => r
                .compilation
                .program
                .as_ref()
                .is_none_or(|p| p.bytes <= limits.file_bytes),
            Self::V2(r) => r.compilation.program.as_ref().is_none_or(|p| {
                // Python reuses read-only source input, not a compiler output file.
                p.kind == v2::ProgramKind::PythonSource
                    || (p
                        .files
                        .iter()
                        .all(|f| f.artifact.bytes <= limits.file_bytes)
                        && p.bytes() <= limits.workdir_bytes)
            }),
        };
        if fits {
            Ok(())
        } else {
            Err("compiled program exceeds output budget")
        }
    }
}
impl StoppedReport {
    pub fn binding(&self) -> &v1::LeaseBinding {
        match self {
            Self::V1(r) => &r.binding,
            Self::V2(r) => &r.binding,
        }
    }
    pub fn cleanup_complete(&self) -> bool {
        match self {
            Self::V1(r) => r.cleanup_complete,
            Self::V2(r) => r.cleanup_complete,
        }
    }
    pub fn validate_for_manifest(&self, manifest: &JobManifest) -> v1::Validation {
        match (self, manifest) {
            (Self::V1(r), JobManifest::V1(_)) => r.validate()?,
            (Self::V2(r), JobManifest::V2(_)) => r.validate()?,
            _ => return Err("stop protocol differs from authorized manifest"),
        }
        if self.binding().evaluation_id != manifest.evaluation_id() {
            return Err("stop belongs to another manifest");
        }
        Ok(())
    }
}
