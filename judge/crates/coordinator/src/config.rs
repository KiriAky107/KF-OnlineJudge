//! Service-only secrets from a private local file; never format database/token values.
use crate::admission::{ResourceBudget, WorkerCapacity};
use kf_contracts::judging::{Digest, Identifier, LeaseToken};
use kf_contracts::judging_versions::NodeAdmission;
use serde::Deserialize;
use std::{collections::HashSet, fs, io, net::SocketAddr, path::Path};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerIdentity {
    pub id: Identifier,
    pub token: LeaseToken,
    #[serde(default = "default_slots")]
    pub slots: u32,
    #[serde(default = "default_task_cpu_millicores")]
    pub task_cpu_millicores: u64,
    #[serde(default)]
    pub resource_budget: Option<ResourceBudget>,
    /// Operator-attested measurement setup; absent means no comparable statistics.
    /// Rotate whenever hardware/kernel/CPU scheduling or measurement setup changes.
    #[serde(default)]
    pub measurement_epoch: Option<Digest>,
}
fn default_slots() -> u32 {
    1
}
fn default_task_cpu_millicores() -> u64 {
    1000
}
impl WorkerIdentity {
    pub fn capacity(&self) -> WorkerCapacity {
        WorkerCapacity {
            slots: self.slots,
            task_cpu_millicores: self.task_cpu_millicores,
            resource_budget: self.resource_budget,
        }
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen: SocketAddr,
    pub database_url: String,
    pub workers: Vec<WorkerIdentity>,
    pub platform_token: LeaseToken,
    pub admission: NodeAdmission,
    pub lease_seconds: u32,
    pub recovery_seconds: u32,
}
impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        let meta = fs::symlink_metadata(path)?;
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if !meta.is_file()
            || meta.nlink() != 1
            || meta.permissions().mode() & 0o777 != 0o600
            || meta.len() > 32768
        {
            return Err(io::Error::other(
                "private coordinator configuration required",
            ));
        }
        let v: Self = serde_json::from_slice(&fs::read(path)?)
            .map_err(|_| io::Error::other("invalid coordinator configuration"))?;
        if !v.listen.ip().is_loopback()
            || !(5..=300).contains(&v.lease_seconds)
            || !(1..=60).contains(&v.recovery_seconds)
            || v.workers.is_empty()
            || v.workers.len() > 64
            || v.workers.iter().map(|w| w.id).collect::<HashSet<_>>().len() != v.workers.len()
            || v.workers
                .iter()
                .any(|w| w.token.expose_secret() == v.platform_token.expose_secret())
        {
            return Err(io::Error::other("invalid coordinator policy"));
        }
        let tokens: HashSet<_> = v.workers.iter().map(|w| w.token.expose_secret()).collect();
        if tokens.len() != v.workers.len() {
            return Err(io::Error::other("ambiguous worker identity"));
        }
        v.admission.validate().map_err(io::Error::other)?;
        for worker in &v.workers {
            worker
                .capacity()
                .validate_admission(&v.admission)
                .map_err(io::Error::other)?;
        }
        Ok(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_worker_identity_defaults_to_one_fixed_slot() {
        let worker: WorkerIdentity = serde_json::from_value(serde_json::json!({
            "id": "11111111-1111-4111-8111-111111111111", "token": "a".repeat(64)
        }))
        .unwrap();
        assert_eq!(worker.slots, 1);
        assert_eq!(worker.task_cpu_millicores, 1000);
        assert!(worker.resource_budget.is_none());
        assert!(worker.capacity().validate().is_ok());
    }
    #[test]
    fn explicit_worker_budget_is_local_operator_configuration() {
        let worker: WorkerIdentity = serde_json::from_value(serde_json::json!({
            "id": "11111111-1111-4111-8111-111111111111", "token": "a".repeat(64),
            "slots": 4, "task_cpu_millicores": 500,
            "resource_budget": { "cpu_millicores": 1500, "memory_bytes": 1073741824 }
        }))
        .unwrap();
        assert_eq!(worker.slots, 4);
        assert_eq!(worker.resource_budget.unwrap().cpu_millicores, 1500);
        assert!(worker.capacity().validate().is_ok());
    }
}
