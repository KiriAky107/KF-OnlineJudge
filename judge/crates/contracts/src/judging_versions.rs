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
