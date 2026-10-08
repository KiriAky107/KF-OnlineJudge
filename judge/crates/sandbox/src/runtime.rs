//! Trusted operator configuration and read-only refusal diagnostics.
//! Validation does not provision directories, reserve identities, apply a
//! resource budget or authenticate the configuration file's ownership.

use crate::probe::EnvironmentObservation;
use crate::spec::{ResourceLimits, SpecPolicy};
use kf_contracts::SchemaVersion;
use serde::{Deserialize, Serialize};
use std::path::Path;

/// Initial support policy, not a claim that version alone proves capability.
pub const MIN_KERNEL: (u16, u16) = (6, 6);
pub const MAX_CONFIG_BYTES: u64 = 65_536;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ImageKind {
    #[serde(rename = "linux_gnu_cxx_x86_64_v1")]
    LinuxGnuCxxX86_64V1,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IdentityPool {
    pub worker_uid: u32,
    pub worker_gid: u32,
    pub first_task_uid: u32,
    pub first_task_gid: u32,
    pub count: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AggregateBudget {
    pub memory_bytes: u64,
    pub supervisor_memory_bytes: u64,
    pub processes: u32,
    pub supervisor_processes: u32,
    pub workdir_bytes: u64,
    pub supervisor_workdir_bytes: u64,
    pub cpu_period_us: u64,
    pub cpu_quota_us: u64,
    pub task_cpu_quota_us: u64,
    pub supervisor_cpu_quota_us: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeConfig {
    pub schema_version: SchemaVersion,
    pub state_root: String,
    pub tasks_root: String,
    pub image_root: String,
    pub image_kind: ImageKind,
    pub delegated_cgroup_root: String,
    pub identities: IdentityPool,
    pub max_concurrent_runs: u32,
    pub per_task_ceiling: ResourceLimits,
    pub aggregate: AggregateBudget,
}

#[derive(Debug, PartialEq, Eq, Serialize)]
pub struct ConfigError {
    pub field: &'static str,
    pub code: &'static str,
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.field, self.code)
    }
}

impl std::error::Error for ConfigError {}

fn invalid(field: &'static str, code: &'static str) -> ConfigError {
    ConfigError { field, code }
}

/// Immutable structural policy; deliberately not an execution permit.
#[derive(Clone, Debug)]
pub struct ValidatedRuntimeConfig(RuntimeConfig);

impl ValidatedRuntimeConfig {
    pub fn config(&self) -> &RuntimeConfig {
        &self.0
    }

    pub fn spec_policy(&self) -> SpecPolicy {
        SpecPolicy::with_limits_ceiling(self.0.per_task_ceiling.clone())
            .expect("validated immutable limits ceiling")
    }

    /// Slot mapping only; the supervisor must lease slots until the old task
    /// tree is empty and cleanup is complete, including after restart.
    pub fn task_identity(&self, slot: u32) -> Option<(u32, u32)> {
        (slot < self.0.max_concurrent_runs).then(|| {
            (
                self.0.identities.first_task_uid + slot,
                self.0.identities.first_task_gid + slot,
            )
        })
    }
}

fn canonical_absolute(value: &str) -> bool {
    value.starts_with('/')
        && value.len() <= 4096
        && value[1..].split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && part.len() <= 255
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
        })
}

fn inside(path: &str, root: &str) -> bool {
    path.strip_prefix(root)
        .is_some_and(|tail| tail.starts_with('/'))
}

fn fits(each: u64, slots: u32, reserve: u64, total: u64) -> bool {
    each.checked_mul(u64::from(slots))
        .and_then(|sum| sum.checked_add(reserve))
        .is_some_and(|required| required <= total)
}

impl RuntimeConfig {
    pub fn validate(self) -> Result<ValidatedRuntimeConfig, ConfigError> {
        for (field, value) in [
            ("state_root", &self.state_root),
            ("tasks_root", &self.tasks_root),
            ("image_root", &self.image_root),
            ("delegated_cgroup_root", &self.delegated_cgroup_root),
        ] {
            if !canonical_absolute(value) {
                return Err(invalid(field, "unsafe_path"));
            }
        }
        if !(inside(&self.state_root, "/var/lib") || inside(&self.state_root, "/srv"))
            || !inside(&self.tasks_root, &self.state_root)
            || !inside(&self.image_root, &self.state_root)
            || self.tasks_root == self.image_root
            || inside(&self.tasks_root, &self.image_root)
            || inside(&self.image_root, &self.tasks_root)
        {
            return Err(invalid("roots", "invalid_layout"));
        }
        let identity_root = format!("{}/identities", self.state_root);
        if [&self.tasks_root, &self.image_root].iter().any(|root| {
            *root == &identity_root || inside(root, &identity_root) || inside(&identity_root, root)
        }) {
            return Err(invalid("roots", "identity_lease_overlap"));
        }
        let capture_root = format!("{}/captures", self.state_root);
        if [&self.tasks_root, &self.image_root].iter().any(|root| {
            *root == &capture_root || inside(root, &capture_root) || inside(&capture_root, root)
        }) {
            return Err(invalid("roots", "capture_lease_overlap"));
        }
        if !inside(&self.delegated_cgroup_root, "/sys/fs/cgroup") {
            return Err(invalid("delegated_cgroup_root", "not_delegated_subtree"));
        }
        if !(1..=32).contains(&self.max_concurrent_runs) {
            return Err(invalid("max_concurrent_runs", "out_of_range"));
        }
        let ids = &self.identities;
        if ids.worker_uid < 1000
            || ids.worker_gid < 1000
            || ids.worker_uid == u32::MAX
            || ids.worker_gid == u32::MAX
            || ids.first_task_uid < 100_000
            || ids.first_task_gid < 100_000
            || ids.count < self.max_concurrent_runs
            || ids.count > 32
        {
            return Err(invalid("identities", "invalid_pool"));
        }
        for (first, worker) in [
            (ids.first_task_uid, ids.worker_uid),
            (ids.first_task_gid, ids.worker_gid),
        ] {
            let Some(end) = first.checked_add(ids.count - 1) else {
                return Err(invalid("identities", "integer_overflow"));
            };
            if end == u32::MAX || (first..=end).contains(&worker) {
                return Err(invalid("identities", "overlapping_or_reserved_identity"));
            }
        }
        SpecPolicy::with_limits_ceiling(self.per_task_ceiling.clone())
            .map_err(|error| invalid(error.field, "invalid_task_ceiling"))?;
        let budget = &self.aggregate;
        if budget.supervisor_memory_bytes < 64 * 1024 * 1024
            || budget.supervisor_processes < 8
            || budget.supervisor_workdir_bytes < 1024 * 1024
            || !(1000..=1_000_000).contains(&budget.cpu_period_us)
            || budget.task_cpu_quota_us < 1000
            || budget.supervisor_cpu_quota_us < 1000
        {
            return Err(invalid("aggregate", "invalid_reservation"));
        }
        for (field, each, reserve, total) in [
            (
                "aggregate.memory_bytes",
                self.per_task_ceiling.memory_bytes,
                budget.supervisor_memory_bytes,
                budget.memory_bytes,
            ),
            (
                "aggregate.processes",
                u64::from(self.per_task_ceiling.processes),
                u64::from(budget.supervisor_processes),
                u64::from(budget.processes),
            ),
            (
                "aggregate.workdir_bytes",
                self.per_task_ceiling.workdir_bytes,
                budget.supervisor_workdir_bytes,
                budget.workdir_bytes,
            ),
            (
                "aggregate.cpu_quota_us",
                budget.task_cpu_quota_us,
                budget.supervisor_cpu_quota_us,
                budget.cpu_quota_us,
            ),
        ] {
            if !fits(each, self.max_concurrent_runs, reserve, total) {
                return Err(invalid(field, "insufficient_or_overflowing_budget"));
            }
        }
        // Conservative first-deployment ceilings, not unlimited host settings.
        if budget.memory_bytes > 64 * 1024 * 1024 * 1024
            || budget.workdir_bytes > 16 * 1024 * 1024 * 1024
            || budget.processes > 8192
            || budget.cpu_quota_us > budget.cpu_period_us * 64
        {
            return Err(invalid("aggregate", "exceeds_service_ceiling"));
        }
        Ok(ValidatedRuntimeConfig(self))
    }
}

#[derive(Debug, Serialize)]
pub struct RefusalReason {
    pub code: &'static str,
    pub subject: &'static str,
}

#[derive(Debug, Serialize)]
pub struct RuntimePreflight {
    pub error: &'static str,
    pub configuration_valid: bool,
    pub execution_allowed: bool,
    pub reasons: Vec<RefusalReason>,
}

pub fn kernel_supported(release: &str) -> Option<bool> {
    let mut parts = release.split('.');
    let major = parts.next()?.parse::<u16>().ok()?;
    let minor = parts.next()?.parse::<u16>().ok()?;
    Some((major, minor) >= MIN_KERNEL)
}

/// Read-only observations. Every privileged capability remains unverified;
/// even a hypothetical complete filesystem does not authorize execution.
pub fn preflight(
    config: &ValidatedRuntimeConfig,
    observation: &EnvironmentObservation,
) -> RuntimePreflight {
    let mut reasons = Vec::new();
    let mut refuse = |code, subject| reasons.push(RefusalReason { code, subject });
    if observation.os != "linux" {
        refuse("unsupported_platform", "os");
    }
    if observation.architecture != "x86_64" {
        refuse("unsupported_platform", "architecture");
    }
    match observation
        .kernel_release
        .as_deref()
        .and_then(kernel_supported)
    {
        Some(true) => {}
        Some(false) => refuse("kernel_below_policy_minimum", "kernel"),
        None => refuse("kernel_unobservable", "kernel"),
    }
    for namespace in ["user", "mnt", "pid", "ipc", "uts", "net", "cgroup"] {
        if !observation
            .namespace_entries
            .iter()
            .any(|entry| entry == namespace)
        {
            refuse("namespace_not_visible", namespace);
        }
    }
    if !observation.cgroup_v2_visible {
        refuse("cgroup_v2_not_visible", "cgroup");
    }
    for controller in ["cpu", "memory", "pids"] {
        if !observation
            .cgroup_v2_controllers
            .iter()
            .any(|entry| entry == controller)
        {
            refuse("controller_not_visible", controller);
        }
    }
    let policy = config.config();
    for (subject, root) in [
        ("state_root", &policy.state_root),
        ("tasks_root", &policy.tasks_root),
        ("image_root", &policy.image_root),
        ("delegated_cgroup_root", &policy.delegated_cgroup_root),
    ] {
        if !Path::new(root).is_dir() {
            refuse("directory_not_visible", subject);
        }
    }
    for interface in [
        "cgroup.type",
        "cgroup.subtree_control",
        "cgroup.kill",
        "cgroup.events",
        "cpu.max",
        "cpu.stat",
        "memory.max",
        "memory.swap.max",
        "memory.peak",
        "memory.events",
        "memory.oom.group",
        "pids.max",
        "pids.events",
    ] {
        if !Path::new(&policy.delegated_cgroup_root)
            .join(interface)
            .is_file()
        {
            refuse("cgroup_interface_not_visible", interface);
        }
    }
    // A controlled directory image includes a GNU C/C++ toolchain, runtime
    // loader and immutable manifest. Content/ownership checks come later.
    for (subject, relative) in [
        ("image_manifest", "manifest.json"),
        ("gcc", "usr/bin/gcc"),
        ("g++", "usr/bin/g++"),
        ("dynamic_loader", "lib64/ld-linux-x86-64.so.2"),
    ] {
        if !Path::new(&policy.image_root).join(relative).is_file() {
            refuse("image_file_not_visible", subject);
        }
    }
    for subject in [
        "configuration_ownership",
        "directory_containment",
        "image_integrity",
        "identity_pool_ownership",
        "helper_privileges",
        "cgroup_delegation",
        "aggregate_budget_enforcement",
        "pidfd_wait",
        "namespace_setup",
        "identity_drop",
        "seccomp_filter",
        "cleanup",
    ] {
        refuse("capability_not_verified", subject);
    }
    refuse("active_validation_required", "isolated_execution");
    RuntimePreflight {
        error: "runtime_not_ready",
        configuration_valid: true,
        execution_allowed: false,
        reasons,
    }
}
