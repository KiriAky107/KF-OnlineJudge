//! Node reservations are held for the whole job, until cleanup is proven.
use kf_contracts::{
    judging as v1, judging_v2 as v2,
    judging_versions::{JobManifest, NodeAdmission},
};
use serde::Deserialize;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ResourceBudget {
    /// CPU quota capacity (1000 = one core), not task CPU duration.
    pub cpu_millicores: u64,
    pub memory_bytes: u64,
}
impl ResourceBudget {
    pub fn for_manifest(manifest: &JobManifest, task_cpu_millicores: u64) -> Self {
        let (compile, run) = match manifest {
            JobManifest::V1(m) => (&m.compile_limits, &m.run_limits),
            JobManifest::V2(m) => (&m.compile_limits, &m.run_limits),
        };
        Self {
            cpu_millicores: task_cpu_millicores,
            memory_bytes: compile.memory_bytes.max(run.memory_bytes),
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub struct WorkerCapacity {
    pub slots: u32,
    /// Must match the native sandbox task quota/period. Not a prediction.
    pub task_cpu_millicores: u64,
    pub resource_budget: Option<ResourceBudget>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionRefusal {
    Slots,
    Cpu,
    Memory,
    JobExceedsBudget,
}
impl WorkerCapacity {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !(1..=64).contains(&self.slots) || self.task_cpu_millicores == 0 {
            return Err("invalid worker capacity");
        }
        if self
            .resource_budget
            .is_some_and(|b| b.cpu_millicores < self.task_cpu_millicores || b.memory_bytes == 0)
        {
            return Err("invalid worker resource budget");
        }
        Ok(())
    }
    /// Every accepted manifest must fit an idle worker. This prevents a
    /// budget mismatch from leaving otherwise valid jobs queued forever.
    pub fn validate_admission(&self, policy: &NodeAdmission) -> Result<(), &'static str> {
        self.validate()?;
        let legacy = |p: &v1::AdmissionPolicy| {
            p.compile_ceiling
                .memory_bytes
                .max(p.run_ceiling.memory_bytes)
        };
        let multi = |p: &v2::AdmissionPolicy| {
            p.capabilities
                .iter()
                .map(|c| {
                    c.compile_ceiling
                        .memory_bytes
                        .max(c.run_ceiling.memory_bytes)
                })
                .max()
                .unwrap_or(0)
        };
        let memory = match policy {
            NodeAdmission::Legacy(p) => legacy(p),
            NodeAdmission::Multi(p) => multi(p),
            NodeAdmission::Registry(p) => p
                .legacy
                .as_ref()
                .map(legacy)
                .unwrap_or(0)
                .max(p.multi_runtime.as_ref().map(multi).unwrap_or(0)),
        };
        if self
            .resource_budget
            .is_some_and(|b| b.memory_bytes < memory)
        {
            return Err("worker budget cannot fit the admitted single-job memory ceiling");
        }
        Ok(())
    }
    pub fn admit(
        &self,
        candidate: ResourceBudget,
        active: &[ResourceBudget],
    ) -> Result<(), AdmissionRefusal> {
        if let Some(budget) = self.resource_budget
            && (candidate.cpu_millicores > budget.cpu_millicores
                || candidate.memory_bytes > budget.memory_bytes)
        {
            return Err(AdmissionRefusal::JobExceedsBudget);
        }
        if active.len() >= self.slots as usize {
            return Err(AdmissionRefusal::Slots);
        }
        if let Some(budget) = self.resource_budget {
            // Compare remaining capacity; no unchecked accumulation/overflow.
            let mut remaining = ResourceBudget {
                cpu_millicores: budget.cpu_millicores - candidate.cpu_millicores,
                memory_bytes: budget.memory_bytes - candidate.memory_bytes,
            };
            for reserved in active {
                remaining.cpu_millicores = remaining
                    .cpu_millicores
                    .checked_sub(reserved.cpu_millicores)
                    .ok_or(AdmissionRefusal::Cpu)?;
                remaining.memory_bytes = remaining
                    .memory_bytes
                    .checked_sub(reserved.memory_bytes)
                    .ok_or(AdmissionRefusal::Memory)?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn capacity() -> WorkerCapacity {
        WorkerCapacity {
            slots: 3,
            task_cpu_millicores: 1000,
            resource_budget: Some(ResourceBudget {
                cpu_millicores: 2000,
                memory_bytes: 600,
            }),
        }
    }
    fn job(memory_bytes: u64) -> ResourceBudget {
        ResourceBudget {
            cpu_millicores: 1000,
            memory_bytes,
        }
    }
    #[test]
    fn fixed_slots_do_not_apply_resource_budget() {
        let c = WorkerCapacity {
            resource_budget: None,
            ..capacity()
        };
        assert_eq!(c.admit(job(500), &[job(500), job(500)]), Ok(()));
        assert_eq!(
            c.admit(job(500), &[job(500); 3]),
            Err(AdmissionRefusal::Slots)
        );
    }
    #[test]
    fn memory_reservation_blocks_then_cleanup_releases() {
        let c = capacity();
        assert_eq!(
            c.admit(job(400), &[job(300)]),
            Err(AdmissionRefusal::Memory)
        );
        assert_eq!(c.admit(job(400), &[]), Ok(()));
        assert_eq!(c.admit(job(300), &[job(300)]), Ok(()));
    }
    #[test]
    fn cpu_quota_capacity_blocks_with_free_slots() {
        assert_eq!(
            capacity().admit(job(10), &[job(10); 2]),
            Err(AdmissionRefusal::Cpu)
        );
    }
    #[test]
    fn oversized_job_is_not_a_waiting_reservation() {
        assert_eq!(
            capacity().admit(job(601), &[]),
            Err(AdmissionRefusal::JobExceedsBudget)
        );
    }
    #[test]
    fn limits_reserve_larger_stage_without_changing_cpu_quota() {
        let mut m: JobManifest = serde_json::from_slice(include_bytes!(
            "../../../../tests/fixtures/contracts/judge-manifest.valid.json"
        ))
        .unwrap();
        if let JobManifest::V1(ref mut m) = m {
            m.compile_limits.memory_bytes = 500;
            m.run_limits.memory_bytes = 900;
            m.compile_limits.cpu_time_us = 30_000_000;
            m.run_limits.cpu_time_us = 1_000_000;
        }
        assert_eq!(ResourceBudget::for_manifest(&m, 1000), job(900));
    }
    #[test]
    fn configuration_rejects_budget_that_cannot_fit_admitted_jobs() {
        let policy: NodeAdmission = serde_json::from_slice(include_bytes!(
            "../../../../tests/fixtures/contracts/judge-admission.valid.json"
        ))
        .unwrap();
        assert!(capacity().validate_admission(&policy).is_err());
        let fitting = WorkerCapacity {
            resource_budget: Some(ResourceBudget {
                cpu_millicores: 2000,
                memory_bytes: 536870912,
            }),
            ..capacity()
        };
        assert!(fitting.validate_admission(&policy).is_ok());
        assert!(
            WorkerCapacity {
                resource_budget: None,
                ..capacity()
            }
            .validate_admission(&policy)
            .is_ok()
        );
    }
    #[test]
    fn configuration_rejects_unusable_capacity() {
        assert!(capacity().validate().is_ok());
        assert!(
            WorkerCapacity {
                slots: 0,
                ..capacity()
            }
            .validate()
            .is_err()
        );
        assert!(
            WorkerCapacity {
                task_cpu_millicores: 3000,
                ..capacity()
            }
            .validate()
            .is_err()
        );
    }
}
