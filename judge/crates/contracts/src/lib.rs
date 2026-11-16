//! Wire contracts only. No OS, networking, database or business orchestration.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub mod judging;
pub mod judging_v2;
pub mod judging_versions;
pub mod restore_io;
pub mod state;

pub const SCHEMA_VERSION: u16 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u16", into = "u16")]
pub struct SchemaVersion;

impl TryFrom<u16> for SchemaVersion {
    type Error = &'static str;

    fn try_from(value: u16) -> Result<Self, Self::Error> {
        if value == SCHEMA_VERSION {
            Ok(Self)
        } else {
            Err("unsupported contract version")
        }
    }
}

impl From<SchemaVersion> for u16 {
    fn from(_: SchemaVersion) -> Self {
        SCHEMA_VERSION
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum TaskKind {
    #[serde(rename = "judge.evaluation")]
    JudgeEvaluation,
}

/// Notification only; consumers must obtain execution authority from the coordinator.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JudgeTaskNotification {
    pub schema_version: SchemaVersion,
    pub kind: TaskKind,
    pub evaluation_id: Uuid,
}
