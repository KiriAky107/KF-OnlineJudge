#![cfg(target_os = "linux")]

//! These tests are skipped by ordinary cargo test. Only an independently capped
//! VM runner may opt in; absence of that environment must fail, never pass.

use kf_sandbox::cgroup::DelegatedCgroup;
use kf_sandbox::launcher::blocked::{BlockedTarget, PreparedTarget};
use kf_sandbox::outcome::{ErrorCode, ExecutionStage};
use kf_sandbox::runtime::{RuntimeConfig, ValidatedRuntimeConfig};
use kf_sandbox::spec::{ExecutionSpec, SpecPolicy, ValidatedSpec};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

static ATTEMPT: AtomicU64 = AtomicU64::new(0);

fn require_vm() {
    assert_eq!(
        std::env::var("KF_SANDBOX_INDEPENDENT_VM").as_deref(),
        Ok("1")
    );
    assert_eq!(
        fs::read_to_string("/kf/INDEPENDENT_VM").unwrap().trim(),
        "isolated-test-guest"
    );
    assert!(
        fs::read_to_string("/sys/class/dmi/id/sys_vendor")
            .unwrap()
            .contains("QEMU")
    );
    for path in ["/mnt/hgfs", "/home/kronecker", "/root/.ssh"] {
        assert!(!Path::new(path).exists());
    }
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    for forbidden in ["hgfs", "9p", "virtiofs"] {
        assert!(!mounts.contains(forbidden));
    }
    let net: Vec<_> = fs::read_dir("/sys/class/net")
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(net, ["lo"]);
}

struct Root {
    path: PathBuf,
    config: RuntimeConfig,
}

impl Root {
    fn new() -> Self {
        require_vm();
        let path = PathBuf::from(format!(
            "/sys/fs/cgroup/kf-vm-{}-{}",
            std::process::id(),
            ATTEMPT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        let mut config: RuntimeConfig = serde_json::from_str(include_str!(
            "../../../../deploy/sandbox.runtime.example.json"
        ))
        .unwrap();
        config.delegated_cgroup_root = path.to_str().unwrap().into();
        config.aggregate.task_cpu_quota_us = 10_000;
        Self { path, config }
    }
    fn validated(&self) -> ValidatedRuntimeConfig {
        self.config.clone().validate().unwrap()
    }
}

impl Drop for Root {
    fn drop(&mut self) {
        if self.path.exists() {
            let _ = fs::write(self.path.join("cgroup.kill"), "1");
            if let Ok(entries) = fs::read_dir(&self.path) {
                for entry in entries.flatten() {
                    if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                        let _ = fs::remove_dir(entry.path());
                    }
                }
            }
            let _ = fs::remove_dir(&self.path);
        }
    }
}

fn spec(id: &str, memory: u64, processes: u32) -> ValidatedSpec {
    let mut spec: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    spec.run_id = id.into();
    spec.limits.memory_bytes = memory;
    spec.limits.processes = processes;
    spec.validate(&SpecPolicy::default()).unwrap()
}

fn binary() -> PathBuf {
    std::env::var_os("KF_SANDBOX_TEST_BIN")
        .expect("VM sandbox path")
        .into()
}
fn children() -> Vec<u32> {
    let mut children = Vec::new();
    for task in fs::read_dir("/proc/self/task").unwrap() {
        children.extend(
            fs::read_to_string(task.unwrap().path().join("children"))
                .unwrap()
                .split_whitespace()
                .map(|pid| pid.parse::<u32>().unwrap()),
        );
    }
    children.sort_unstable();
    children
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn stopped_target_is_enrolled_frozen_and_reaped_before_namespace_refusal() {
    let root = Root::new();
    let target =
        PreparedTarget::prepare(&root.validated(), &spec("gated", 64 << 20, 8), &binary()).unwrap();
    let pid = target.pid();
    let path = root.path.join(target.cgroup().name());
    let limits = target.cgroup().applied_limits().unwrap();
    assert_eq!(limits.memory_bytes, 64 << 20);
    assert_eq!(limits.swap_bytes, 0);
    assert_eq!(limits.processes, 8);
    assert_eq!(
        (limits.cpu_quota_us, limits.cpu_period_us),
        (10_000, 100_000)
    );
    assert!(limits.oom_group);
    for _ in 0..3 {
        assert!(
            fs::read_to_string(format!("/proc/{pid}/status"))
                .unwrap()
                .contains("State:\tT (stopped)")
        );
        assert_eq!(
            fs::read_to_string(path.join("cgroup.freeze"))
                .unwrap()
                .trim(),
            "1"
        );
        assert_eq!(
            fs::read_to_string(path.join("cgroup.procs"))
                .unwrap()
                .trim(),
            pid.to_string()
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let failure = target.refuse_unimplemented_isolation();
    assert_eq!(failure.failure.stage, ExecutionStage::Namespaces);
    assert_eq!(failure.failure.code, ErrorCode::CapabilityUnavailable);
    assert!(failure.cleanup_failure.is_none());
    assert!(!Path::new(&format!("/proc/{pid}")).exists());
    assert!(!path.exists());
    println!(
        "KF_GATED_PROOF pid={pid} memory={} swap=0 pids=8 cpu=10000/100000 frozen=1 refusal=namespaces reaped=1 removed=1",
        limits.memory_bytes
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn unsafe_delegation_fails_at_cgroup_and_does_not_leave_bootstrap() {
    let root = Root::new();
    let before = children();
    fs::set_permissions(&root.path, fs::Permissions::from_mode(0o777)).unwrap();
    let result =
        PreparedTarget::prepare(&root.validated(), &spec("unsafe", 64 << 20, 8), &binary());
    let Err(failure) = result else {
        panic!("unsafe root must fail");
    };
    assert_eq!(failure.failure.stage, ExecutionStage::Cgroup);
    assert!(failure.cleanup_failure.is_none());
    assert_eq!(children(), before);
    assert!(!root.path.join("kf-unsafe").exists());
    println!("KF_CGROUP_FAILURE_PROOF stage=cgroup bootstrap_reaped=1");
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn exclusive_tasks_and_kernel_directory_count_prevent_adoption_and_overbooking() {
    let root = Root::new();
    let config = root.validated();
    let delegate = DelegatedCgroup::open(&config).unwrap();
    let mut one = delegate.create_task(&spec("one", 64 << 20, 8)).unwrap();
    assert!(delegate.create_task(&spec("one", 64 << 20, 8)).is_err());
    let mut two = delegate.create_task(&spec("two", 64 << 20, 8)).unwrap();
    assert!(delegate.create_task(&spec("three", 64 << 20, 8)).is_err());
    assert_eq!(one.applied_limits().unwrap().memory_bytes, 64 << 20);
    one.kill_and_remove(Duration::from_secs(1)).unwrap();
    one.kill_and_remove(Duration::from_secs(1)).unwrap();
    two.kill_and_remove(Duration::from_secs(1)).unwrap();
    assert!(!root.path.join("kf-one").exists());
    assert!(!root.path.join("kf-two").exists());
    println!("KF_EXCLUSIVE_PROOF collision_rejected=1 max_tasks=2 idempotent_cleanup=1");
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn changed_limits_are_rejected_while_child_is_still_stopped() {
    let root = Root::new();
    let delegate = DelegatedCgroup::open(&root.validated()).unwrap();
    let mut group = delegate.create_task(&spec("changed", 64 << 20, 8)).unwrap();
    let path = root.path.join(group.name());
    fs::write(path.join("memory.max"), (32 << 20).to_string()).unwrap();
    let mut child = BlockedTarget::spawn(&binary()).unwrap();
    assert!(group.enroll(&mut child).is_err());
    child.verify_stopped().unwrap();
    assert!(
        fs::read_to_string(path.join("cgroup.procs"))
            .unwrap()
            .trim()
            .is_empty()
    );
    group.kill_and_remove(Duration::from_secs(1)).unwrap();
    assert_eq!(child.terminate().unwrap().signal(), Some(9));
    println!("KF_CHANGED_LIMIT_PROOF enrollment_rejected=1 blocked=1 terminated=1");
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn request_cannot_exceed_operator_ceiling() {
    let root = Root::new();
    let delegate = DelegatedCgroup::open(&root.validated()).unwrap();
    assert!(
        delegate
            .create_task(&spec("excess", 1024 << 20, 8))
            .is_err()
    );
    assert!(!root.path.join("kf-excess").exists());
    println!("KF_CEILING_PROOF request_excess_rejected=1");
}

fn counter(raw: &str, key: &str) -> u64 {
    raw.lines()
        .find_map(|line| {
            line.split_once(' ')
                .filter(|(name, _)| *name == key)
                .map(|(_, value)| value.parse().unwrap())
        })
        .unwrap()
}

fn limit_probe(kind: &str) {
    let root = Root::new();
    let delegate = DelegatedCgroup::open(&root.validated()).unwrap();
    let processes = if kind == "pids" { 1 } else { 8 };
    let mut task = delegate
        .create_task(&spec(kind, 32 << 20, processes))
        .unwrap();
    let path = root.path.join(task.name());
    let mut child = BlockedTarget::spawn(Path::new(&format!("/kf/bin/limit-{kind}"))).unwrap();
    task.enroll(&mut child).unwrap();
    // VM-only fixed probes: deliberately exercise controllers after admission.
    // Production APIs expose no thaw/continue or user execution permit.
    fs::write(path.join("cgroup.freeze"), "0").unwrap();
    let pid = rustix::process::Pid::from_raw(child.pid() as i32).unwrap();
    let pidfd = rustix::process::pidfd_open(pid, rustix::process::PidfdFlags::empty()).unwrap();
    rustix::process::pidfd_send_signal(&pidfd, rustix::process::Signal::CONT).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.poll_exit().unwrap() {
            break status;
        }
        assert!(Instant::now() < deadline, "bounded fixed resource probe");
        std::thread::sleep(Duration::from_millis(5));
    };
    let cpu = fs::read_to_string(path.join("cpu.stat")).unwrap();
    let memory = fs::read_to_string(path.join("memory.events")).unwrap();
    let pids = fs::read_to_string(path.join("pids.events")).unwrap();
    match kind {
        "pids" => {
            assert!(status.success());
            assert!(counter(&pids, "max") >= 1);
        }
        "memory" => {
            assert_eq!(status.signal(), Some(9));
            assert!(counter(&memory, "oom_kill") >= 1);
        }
        "cpu" => {
            assert!(status.success());
            assert!(counter(&cpu, "usage_usec") >= 150_000);
            assert!(counter(&cpu, "nr_throttled") >= 1);
        }
        _ => unreachable!(),
    }
    println!(
        "KF_LIMIT_PROOF kind={kind} status={status} cpu={cpu:?} memory={memory:?} pids={pids:?}"
    );
    task.kill_and_remove(Duration::from_secs(1)).unwrap();
    assert!(!path.exists());
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn pids_controller_actually_denies_fork() {
    limit_probe("pids");
}
#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn memory_controller_actually_kills_at_limit() {
    limit_probe("memory");
}
#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn cpu_controller_actually_throttles_fixed_work() {
    limit_probe("cpu");
}
