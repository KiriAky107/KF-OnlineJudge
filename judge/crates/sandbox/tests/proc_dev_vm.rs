#![cfg(target_os = "linux")]
//! Real restricted proc/dev verification, only in an independently capped VM.
mod support;
use kf_sandbox::launcher::proc_dev::RestrictedRootTarget;
use kf_sandbox::outcome::ExecutionStage;
use std::fs;
use std::path::Path;
use std::process::Command;
use support::{Root, binary, children, spec};

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn restricted_proc_devices_and_root_boundaries_are_real() {
    let root = Root::new();
    let before = fs::read_to_string("/proc/self/mountinfo").unwrap();
    let one =
        RestrictedRootTarget::prepare(&root.validated(), &spec("proc-one", 64 << 20, 8), &binary())
            .unwrap();
    let two =
        RestrictedRootTarget::prepare(&root.validated(), &spec("proc-two", 64 << 20, 8), &binary())
            .unwrap();
    let a = format!("/proc/{}/root", one.root().init_pid());
    let b = format!("/proc/{}/root", two.root().init_pid());
    fs::write(format!("{b}/work/peer-only"), "isolated peer").unwrap();
    assert!(!Path::new(&format!("{a}/work/peer-only")).exists());
    let output = Command::new("/kf/bin/proc-dev-probe")
        .arg(one.root().init_pid().to_string())
        .arg(two.root().init_pid().to_string())
        .env_clear()
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixed proc/dev probe: {:?} {:?}",
        output.stdout,
        output.stderr
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "KF_PROC_DEV_PROBE pid_subset=1 hidepid=1 devices=1 old_root_absent=1 readonly=1"
    );
    for target in [&one, &two] {
        let info =
            fs::read_to_string(format!("/proc/{}/mountinfo", target.root().init_pid())).unwrap();
        assert_eq!(
            info.lines().count(),
            5,
            "only root/work/tmp/proc/dev: {info}"
        );
        assert!(!info.contains(" shared:") && !info.contains(" master:"));
        for pid in [target.root().init_pid(), target.root().bootstrap_pid()] {
            for fd in fs::read_dir(format!("/proc/{pid}/fd")).unwrap() {
                assert!(!fs::metadata(fd.unwrap().path()).unwrap().is_dir());
            }
        }
    }
    assert_eq!(fs::read_to_string("/proc/self/mountinfo").unwrap(), before);
    for (target, id) in [(one, "proc-one"), (two, "proc-two")] {
        let group = root.path.join(target.root().cgroup().name());
        let error = target.refuse_remaining_isolation();
        assert_eq!(error.failure.stage, ExecutionStage::RootFilesystem);
        assert!(error.cleanup_failure.is_none());
        assert!(!group.exists());
        assert!(!Path::new(&format!("/var/lib/kf-judge/tasks/kf-{id}")).exists());
    }
    println!(
        "KF_PROC_DEV_PROOF fresh_pid_proc=1 readonly=1 fixed_devices=1 init_and_bootstrap_no_directory_fds=1 peer_isolated=1 host_mounts_unchanged=1 refusal=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn unexpected_inherited_file_and_directory_fds_refuse_initialization() {
    let root = Root::new();
    let before = children();
    for (path, id) in [
        ("/image-outside-sentinel", "proc-file-fd"),
        ("/var/lib/kf-judge/tasks", "proc-dir-fd"),
    ] {
        let leak = fs::File::open(path).unwrap();
        rustix::io::fcntl_setfd(&leak, rustix::io::FdFlags::empty()).unwrap();
        let Err(error) =
            RestrictedRootTarget::prepare(&root.validated(), &spec(id, 64 << 20, 8), &binary())
        else {
            panic!("unexpected inherited FD must fail closed");
        };
        assert_eq!(error.failure.stage, ExecutionStage::Preparation);
        assert!(error.cleanup_failure.is_none());
        assert!(!Path::new(&format!("/var/lib/kf-judge/tasks/kf-{id}")).exists());
        assert!(!root.path.join(format!("kf-{id}")).exists());
        drop(leak);
    }
    assert_eq!(children(), before);
    println!(
        "KF_PROC_DEV_FD_PROOF unknown_file_and_directory_rejected=1 no_namespace_started=1 task_removed=1 child_reaped=1"
    );
}

#[test]
#[ignore = "requires independent VM and fixed mknodat-denying test launcher"]
fn kernel_mknod_failure_rolls_back_proc_dev_and_root() {
    let root = Root::new();
    assert_eq!(std::env::var("KF_SANDBOX_DENY_MKNOD").as_deref(), Ok("1"));
    let before = children();
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    let Err(error) = RestrictedRootTarget::prepare(
        &root.validated(),
        &spec("proc-mknod-fail", 64 << 20, 8),
        &binary(),
    ) else {
        panic!("kernel mknodat denial must fail root stage");
    };
    assert_eq!(error.failure.stage, ExecutionStage::RootFilesystem);
    assert_eq!(error.failure.errno, Some(1));
    assert!(error.cleanup_failure.is_none());
    assert_eq!(children(), before);
    assert!(!Path::new("/var/lib/kf-judge/tasks/kf-proc-mknod-fail").exists());
    assert!(!root.path.join("kf-proc-mknod-fail").exists());
    assert_eq!(fs::read_to_string("/proc/self/mountinfo").unwrap(), mounts);
    println!(
        "KF_PROC_DEV_FAILURE_PROOF errno=1 partial_proc_and_dev_cleanup=1 task_root_removed=1 bootstrap_reaped=1 host_mounts_unchanged=1"
    );
}
