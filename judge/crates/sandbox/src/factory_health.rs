//! Versioned read-only factory observation, never an isolation/execution permit.
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FactoryHealth {
    pub schema_version: u8,
    pub configuration_sha256: String,
    pub supervisor_authorized: bool,
    pub execution_gate: String,
    pub isolation_verified: bool,
    pub max_concurrent_runs: u32,
    pub active_helpers: u32,
    pub cleanup_complete: bool,
    pub remaining_requests: u32,
    pub remaining_lifetime_seconds: u64,
}
impl FactoryHealth {
    pub(crate) fn valid(&self) -> bool {
        self.schema_version == 1
            && self.configuration_sha256.len() == 64
            && self
                .configuration_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            && self.supervisor_authorized
            && self.execution_gate == "required_per_execution"
            && !self.isolation_verified
            && (1..=32).contains(&self.max_concurrent_runs)
            && self.active_helpers <= self.max_concurrent_runs
            && (!self.cleanup_complete || self.active_helpers == 0)
            && self.remaining_requests <= super::factory_cycles::MAX_IDS as u32
            && self.remaining_lifetime_seconds <= 86400
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn health_never_certifies_namespace_execution_or_busy_cleanup() {
        let mut health = FactoryHealth {
            schema_version: 1,
            configuration_sha256: "b".repeat(64),
            supervisor_authorized: true,
            execution_gate: "required_per_execution".into(),
            isolation_verified: false,
            max_concurrent_runs: 1,
            active_helpers: 0,
            cleanup_complete: true,
            remaining_requests: 4096,
            remaining_lifetime_seconds: 3600,
        };
        assert!(health.valid());
        health.isolation_verified = true;
        assert!(!health.valid());
        health.isolation_verified = false;
        health.active_helpers = 1;
        assert!(!health.valid());
        health.cleanup_complete = false;
        assert!(health.valid());
    }
}
