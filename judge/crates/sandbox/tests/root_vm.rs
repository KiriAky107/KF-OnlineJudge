#![cfg(target_os = "linux")]
//! Real root/pivot acceptance requires the externally capped independent VM.
mod support;
use kf_sandbox::launcher::root::RootedTarget;
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
            "VM init must reap orphaned helper"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn task_path(id: &str) -> String {
    format!("/var/lib/kf-judge/tasks/kf-{id}")
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn real_pivot_readonly_image_and_independent_bounded_scratch_are_verified() {
    let root = Root::new();
    let before = fs::read_to_string("/proc/self/mountinfo").unwrap();
    let one = RootedTarget::prepare(&root.validated(), &spec("root-one", 64 << 20, 8), &binary())
        .unwrap();
    let two = RootedTarget::prepare(&root.validated(), &spec("root-two", 64 << 20, 8), &binary())
        .unwrap();
    let output = Command::new("/kf/bin/root-probe")
        .arg(one.init_pid().to_string())
        .env_clear()
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixed root probe failed: {:?} {:?}",
        output.stdout,
        output.stderr
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "KF_ROOT_PROBE readonly=1 old_root_absent=1 scratch=1 tmp_enospc=1"
    );
    let a = format!("/proc/{}/root", one.init_pid());
    let b = format!("/proc/{}/root", two.init_pid());
    assert!(Path::new(&format!("{a}/work/isolated-sentinel")).exists());
    assert!(!Path::new(&format!("{b}/work/isolated-sentinel")).exists());
    assert!(
        !Path::new("/var/lib/kf-judge/images/gnu-cxx-x86_64-v1/work/isolated-sentinel").exists()
    );
    for target in [&one, &two] {
        assert_eq!(
            target.budget().work_bytes + target.budget().tmp_bytes,
            64 << 20
        );
        assert!(
            fs::read_to_string(format!("/proc/{}/status", target.init_pid()))
                .unwrap()
                .contains("State:\tT (stopped)")
        );
        let mountinfo =
            fs::read_to_string(format!("/proc/{}/mountinfo", target.init_pid())).unwrap();
        assert_eq!(
            mountinfo.lines().count(),
            3,
            "root/work/tmp only: {mountinfo}"
        );
        assert!(mountinfo.lines().any(|l| {
            l.split_whitespace().nth(4) == Some("/")
                && l.split_whitespace()
                    .nth(5)
                    .unwrap()
                    .split(',')
                    .any(|f| f == "ro")
        }));
        assert!(!mountinfo.contains(" shared:") && !mountinfo.contains(" master:"));
        for entry in fs::read_dir(format!("/proc/{}/fd", target.init_pid())).unwrap() {
            assert!(!fs::metadata(entry.unwrap().path()).unwrap().is_dir());
        }
        assert_eq!(
            fs::read_to_string(root.path.join(target.cgroup().name()).join("cgroup.freeze"))
                .unwrap()
                .trim(),
            "1"
        );
    }
    assert_eq!(fs::read_to_string("/proc/self/mountinfo").unwrap(), before);
    let ids = (one.init_pid(), two.init_pid());
    for (target, id) in [(one, "root-one"), (two, "root-two")] {
        let bootstrap = target.bootstrap_pid();
        let cgroup = root.path.join(target.cgroup().name());
        let failure = target.refuse_remaining_isolation();
        assert_eq!(failure.failure.stage, ExecutionStage::RootFilesystem);
        assert!(failure.cleanup_failure.is_none());
        assert!(!cgroup.exists());
        assert!(!Path::new(&task_path(id)).exists());
        gone(bootstrap);
    }
    gone(ids.0);
    gone(ids.1);
    println!(
        "KF_ROOT_PROOF init_pids={ids:?} pivot_root=1 ro_image=1 work_tmp_independent=1 bounded_tmp=1 no_directory_fds=1 only_three_mounts=1 host_mounts_unchanged=1 refusal=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn namespace_failure_rolls_back_the_exclusive_root_directory() {
    let root = Root::new();
    let before = children();
    let result = RootedTarget::prepare(
        &root.validated(),
        &spec("root-ns-fail", 64 << 20, 1),
        &binary(),
    );
    let Err(failure) = result else {
        panic!("PID 1 creation must fail at pids.max=1");
    };
    assert_eq!(failure.failure.stage, ExecutionStage::Namespaces);
    assert_eq!(failure.failure.errno, Some(11));
    assert!(failure.cleanup_failure.is_none());
    assert!(!Path::new(&task_path("root-ns-fail")).exists());
    assert!(!root.path.join("kf-root-ns-fail").exists());
    assert_eq!(children(), before);
    println!(
        "KF_ROOT_ROLLBACK_PROOF namespace_failure=11 root_directory_removed=1 bootstrap_reaped=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn unsafe_image_is_rejected_before_spawning_and_existing_tasks_are_not_adopted() {
    let mut root = Root::new();
    let before = children();
    root.config.image_root = "/var/lib/kf-judge/images/unsafe".into();
    let result = RootedTarget::prepare(
        &root.validated(),
        &spec("root-unsafe", 64 << 20, 8),
        &binary(),
    );
    let Err(failure) = result else {
        panic!("unsafe image must fail");
    };
    assert_eq!(failure.failure.stage, ExecutionStage::RootFilesystem);
    assert!(failure.cleanup_failure.is_none());
    assert_eq!(children(), before);
    assert!(!Path::new(&task_path("root-unsafe")).exists());
    root.config.image_root = "/var/lib/kf-judge/images/gnu-cxx-x86_64-v1".into();
    let target = RootedTarget::prepare(
        &root.validated(),
        &spec("root-collision", 64 << 20, 8),
        &binary(),
    )
    .unwrap();
    assert!(
        RootedTarget::prepare(
            &root.validated(),
            &spec("root-collision", 64 << 20, 8),
            &binary()
        )
        .is_err()
    );
    assert!(Path::new(&format!("/proc/{}/root", target.init_pid())).exists());
    drop(target);
    assert!(!Path::new(&task_path("root-collision")).exists());
    println!("KF_ROOT_POLICY_PROOF unsafe_image_rejected=1 exclusive_lease=1 drop_cleanup=1");
}
