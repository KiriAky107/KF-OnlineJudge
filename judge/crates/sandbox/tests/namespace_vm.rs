#![cfg(target_os = "linux")]
//! Privileged acceptance runs exclusively under the independent VM harness.
mod support;

use kf_sandbox::launcher::namespaces::{NamespaceIds, NamespacedTarget};
use kf_sandbox::outcome::ExecutionStage;
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};
use support::{Root, binary, children, spec};

fn gone(pid: u32) {
    let deadline = Instant::now() + Duration::from_secs(2);
    while Path::new(&format!("/proc/{pid}")).exists() {
        assert!(
            Instant::now() < deadline,
            "VM init has not reaped orphaned helper"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn actual_namespaces_and_pid_one_are_distinct_and_remain_gated() {
    let root = Root::new();
    let before = NamespaceIds::observe(std::process::id()).unwrap();
    let one = NamespacedTarget::prepare(&root.validated(), &spec("ns-one", 64 << 20, 8), &binary())
        .unwrap();
    let two = NamespacedTarget::prepare(&root.validated(), &spec("ns-two", 64 << 20, 8), &binary())
        .unwrap();
    let a = one.namespace_ids();
    let b = two.namespace_ids();
    assert_ne!(a.mount, b.mount);
    assert_ne!(a.pid, b.pid);
    assert_ne!(a.ipc, b.ipc);
    assert_ne!(a.uts, b.uts);
    assert_ne!(a.network, b.network);
    assert_ne!(a.cgroup, b.cgroup);
    assert_eq!(a.user, before.user);
    assert_eq!(b.user, before.user);
    let output = Command::new("/kf/bin/namespace-probe")
        .arg(one.init_pid().to_string())
        .env_clear()
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixed probe: {:?} {:?}",
        output.stdout,
        output.stderr
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "KF_NAMESPACE_PROBE uts=1 ipc=1 net_lo_down=1 mount_private=1 pid_child=1"
    );
    assert!(!Path::new("/kf/ns-mount/sentinel").exists());
    assert!(
        Path::new(&format!(
            "/proc/{}/root/kf/ns-mount/sentinel",
            one.init_pid()
        ))
        .exists()
    );
    assert!(
        !Path::new(&format!(
            "/proc/{}/root/kf/ns-mount/sentinel",
            two.init_pid()
        ))
        .exists()
    );
    for target in [&one, &two] {
        let path = root.path.join(target.cgroup().name());
        assert_eq!(
            fs::read_to_string(path.join("cgroup.freeze"))
                .unwrap()
                .trim(),
            "1"
        );
        let status = fs::read_to_string(format!("/proc/{}/status", target.init_pid())).unwrap();
        assert!(status.contains("State:\tT (stopped)"));
        assert!(
            status
                .lines()
                .find_map(|l| l.strip_prefix("NSpid:"))
                .unwrap()
                .split_whitespace()
                .last()
                == Some("1")
        );
        let mounts = fs::read_to_string(format!("/proc/{}/mountinfo", target.init_pid())).unwrap();
        assert!(!mounts.contains(" shared:") && !mounts.contains(" master:"));
        let limits = target.cgroup().applied_limits().unwrap();
        assert_eq!(limits.memory_bytes, 64 << 20);
        assert_eq!(limits.processes, 8);
    }
    assert_eq!(NamespaceIds::observe(std::process::id()).unwrap(), before);
    let ids = (one.init_pid(), two.init_pid());
    for target in [one, two] {
        let bootstrap = target.bootstrap_pid();
        let path = root.path.join(target.cgroup().name());
        let failure = target.refuse_unimplemented_root();
        assert_eq!(failure.failure.stage, ExecutionStage::RootFilesystem);
        assert!(failure.cleanup_failure.is_none());
        assert!(!path.exists());
        gone(bootstrap);
    }
    gone(ids.0);
    gone(ids.1);
    println!(
        "KF_NAMESPACE_PROOF init_pids={ids:?} six_namespaces_distinct=1 user_namespace_unchanged=1 frozen=1 root_refusal=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn pid_limit_initialization_failure_reports_namespace_stage_and_rolls_back() {
    let root = Root::new();
    let before = children();
    let result =
        NamespacedTarget::prepare(&root.validated(), &spec("ns-fail", 64 << 20, 1), &binary());
    let Err(failure) = result else {
        panic!("PID 1 must not bypass pids.max");
    };
    assert_eq!(failure.failure.stage, ExecutionStage::Namespaces);
    assert_eq!(failure.failure.errno, Some(11));
    assert!(failure.cleanup_failure.is_none());
    assert_eq!(children(), before);
    assert!(!root.path.join("kf-ns-fail").exists());
    println!("KF_NAMESPACE_FAILURE_PROOF errno=11 stage=namespaces reaped=1 removed=1");
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn dropping_frozen_namespace_target_kills_helpers_and_removes_task() {
    let root = Root::new();
    let target =
        NamespacedTarget::prepare(&root.validated(), &spec("ns-drop", 64 << 20, 8), &binary())
            .unwrap();
    let ids = (target.bootstrap_pid(), target.init_pid());
    let path = root.path.join(target.cgroup().name());
    drop(target);
    gone(ids.0);
    gone(ids.1);
    assert!(!path.exists());
    println!("KF_NAMESPACE_DROP_PROOF helpers_gone=1 task_removed=1");
}
