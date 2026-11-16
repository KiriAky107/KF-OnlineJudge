//! Private offline restore protocol. No OS operations or execution authority.

use crate::SchemaVersion;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

pub const MAX_FILES: usize = 131072;
pub const MAX_LOGICAL_BYTES: u64 = 12 * 1024 * 1024 * 1024;
pub const MAX_FILE_BYTES: u64 = 256 * 1024 * 1024;
pub const MAX_BATCH_BYTES: u64 = 64 * 1024 * 1024;
pub const MAX_REQUEST_BYTES: usize = 256 * 1024;
pub const MAX_REPLY_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RestoreFile {
    pub path: String,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
    pub bytes: u64,
    pub sha256: String,
}

impl RestoreFile {
    pub fn validate(&self) -> Result<(), &'static str> {
        let mut parts = self.path.split('/');
        if self.path.len() > 512
            || !matches!(
                parts.next(),
                Some(
                    "postgres"
                        | "artifacts"
                        | "imports"
                        | "app-config"
                        | "judge-config"
                        | "factory"
                        | "operations"
                        | "releases"
                        | "images"
                        | "dependencies"
                        | "proxy"
                        | "recovery"
                )
            )
        {
            return Err("restore file domain differs");
        }
        let mut count = 0;
        for part in parts {
            count += 1;
            if part.is_empty()
                || part.len() > 255
                || matches!(part, "." | ".." | ".git" | ".ssh" | ".localplan" | "docs")
                || !part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.+@-".contains(&b))
            {
                return Err("restore path component differs");
            }
        }
        if count == 0
            || !matches!(self.uid, 0 | 1200..=1205)
            || !(if self.uid == 0 {
                matches!(self.gid, 0 | 1203..=1205)
            } else {
                self.gid == self.uid
            })
            || !matches!(
                self.mode,
                0o400 | 0o444 | 0o600 | 0o640 | 0o644 | 0o555 | 0o755
            )
            || self.bytes > MAX_FILE_BYTES
            || self.sha256.len() != 64
            || !self
                .sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || self.sha256 == "0".repeat(64)
            || self.sha256 == "a".repeat(64)
        {
            return Err("restore member metadata differs");
        }
        Ok(())
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum Request {
    #[serde(rename = "restore.io.open")]
    Open {
        schema_version: SchemaVersion,
        backup_id: String,
        recovery_id: String,
        expected_files: u32,
        expected_logical_bytes: u64,
        deadline_monotonic_ns: u64,
    },
    #[serde(rename = "restore.io.batch")]
    Batch {
        schema_version: SchemaVersion,
        batch_id: u32,
        files: Vec<RestoreFile>,
    },
    #[serde(rename = "restore.io.finish")]
    Finish { schema_version: SchemaVersion },
}

pub fn identifier(value: &str, prefix: &str) -> bool {
    value.len() == prefix.len() + 32
        && value.starts_with(prefix)
        && value[prefix.len()..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

impl Request {
    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::Open {
                backup_id,
                recovery_id,
                expected_files,
                expected_logical_bytes,
                deadline_monotonic_ns,
                ..
            } => {
                if !identifier(backup_id, "bk-")
                    || !identifier(recovery_id, "rr-")
                    || !(1..=MAX_FILES as u32).contains(expected_files)
                    || *expected_logical_bytes > MAX_LOGICAL_BYTES
                    || !(1..=i64::MAX as u64).contains(deadline_monotonic_ns)
                {
                    return Err("restore open bounds differ");
                }
            }
            Self::Batch {
                batch_id, files, ..
            } => {
                if !(1..=MAX_FILES as u32).contains(batch_id)
                    || files.is_empty()
                    || files.len() > 256
                {
                    return Err("restore batch count differs");
                }
                let mut seen = HashSet::new();
                for file in files {
                    file.validate()?;
                    if !seen.insert(&file.path) {
                        return Err("duplicate restore file");
                    }
                }
                if files.len() > 1 && files.iter().map(|f| f.bytes).sum::<u64>() > MAX_BATCH_BYTES {
                    return Err("restore batch byte bound exceeded");
                }
            }
            Self::Finish { .. } => {}
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    Opened,
    Verified,
    Finished,
    Refused,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Error {
    None,
    Protocol,
    Guard,
    Deadline,
    Filesystem,
    Bytes,
    Identity,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reply {
    pub schema_version: SchemaVersion,
    pub kind: ReplyKind,
    pub stage: Stage,
    pub batch_id: u32,
    pub verified_files: u32,
    pub verified_logical_bytes: u64,
    pub execution_verified: bool,
    pub error: Error,
}

#[derive(Debug, Serialize, Deserialize)]
pub enum ReplyKind {
    #[serde(rename = "restore.io.reply")]
    RestoreIO,
}

impl Reply {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.execution_verified
            || self.batch_id > MAX_FILES as u32
            || self.verified_files > MAX_FILES as u32
            || self.verified_logical_bytes > MAX_LOGICAL_BYTES
            || (self.stage == Stage::Refused) != (self.error != Error::None)
            || (self.stage == Stage::Opened
                && (self.batch_id != 0
                    || self.verified_files != 0
                    || self.verified_logical_bytes != 0))
            || (matches!(self.stage, Stage::Verified | Stage::Finished)
                && !(1..=self.verified_files).contains(&self.batch_id))
        {
            return Err("restore reply state differs");
        }
        Ok(())
    }
}

pub fn decode_request(raw: &[u8]) -> Result<Request, &'static str> {
    if raw.len() > MAX_REQUEST_BYTES {
        return Err("restore request too large");
    }
    let value: Request = serde_json::from_slice(raw).map_err(|_| "invalid restore request")?;
    value.validate()?;
    Ok(value)
}

pub fn decode_reply(raw: &[u8]) -> Result<Reply, &'static str> {
    if raw.len() > MAX_REPLY_BYTES {
        return Err("restore reply too large");
    }
    let value: Reply = serde_json::from_slice(raw).map_err(|_| "invalid restore reply")?;
    value.validate()?;
    Ok(value)
}
