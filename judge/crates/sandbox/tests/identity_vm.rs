#![cfg(target_os = "linux")]
mod support;
use kf_sandbox::artifacts::ArtifactRoot;
use kf_sandbox::launcher::identity::IdentityReducedTarget;
use kf_sandbox::outcome::ExecutionStage;
use std::os::unix::fs::PermissionsExt;
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
use support::{Root, binary, children};
const LEASES: &str = "/var/lib/kf-judge/identities";
struct Publications(PathBuf);
impl Publications {
    fn new() -> Self {
        support::require_vm();
        let path = PathBuf::from(format!("/kf/identity-publications-{}", std::process::id()));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(path.join("program-001"), b"fixed-non-task-payload").unwrap();
        fs::write(path.join("input-001"), b"private-stdin").unwrap();
        Self(path)
    }
    fn root(&self) -> ArtifactRoot {
        ArtifactRoot::from_directory(
            rustix::fs::open(
                &self.0,
                rustix::fs::OFlags::PATH
                    | rustix::fs::OFlags::DIRECTORY
                    | rustix::fs::OFlags::CLOEXEC,
                rustix::fs::Mode::empty(),
            )
            .unwrap(),
        )
        .unwrap()
    }
}
impl Drop for Publications {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn prepare(
    root: &Root,
    publications: &Publications,
    id: &str,
) -> Result<IdentityReducedTarget, kf_sandbox::launcher::blocked::InitializationFailure> {
    IdentityReducedTarget::prepare(
        &root.validated(),
        &support::spec(id, 64 << 20, 8),
        &binary(),
        &publications.root(),
    )
}
fn leases() -> Vec<String> {
    let mut result: Vec<_> = fs::read_dir(LEASES)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    result.sort();
    result
}
fn verify_ids(target: &IdentityReducedTarget) {
    let (uid, gid) = target.identity();
    let status = fs::read_to_string(format!("/proc/{}/status", target.root().init_pid())).unwrap();
    for (name, id) in [("Uid:", uid), ("Gid:", gid)] {
        let line = status.lines().find(|l| l.starts_with(name)).unwrap();
        assert_eq!(
            line.split_whitespace()
                .skip(1)
                .map(|s| s.parse::<u32>().unwrap())
                .collect::<Vec<_>>(),
            vec![id; 4]
        );
    }
    for name in ["CapInh:", "CapPrm:", "CapEff:", "CapBnd:", "CapAmb:"] {
        assert_eq!(
            u64::from_str_radix(
                status
                    .lines()
                    .find(|l| l.starts_with(name))
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap(),
                16
            )
            .unwrap(),
            0
        );
    }
    assert_eq!(
        status
            .lines()
            .find(|l| l.starts_with("Groups:"))
            .unwrap()
            .split_whitespace()
            .count(),
        1
    );
    assert!(status.contains("NoNewPrivs:\t1\n"));
    assert!(target.root().snapshot_outputs().unwrap().is_empty());
}
#[test]
#[ignore = "requires independent capped VM and fixed credential inheritance fixture"]
fn production_init_reduces_credentials_and_leases_distinct_ids_until_cleanup() {
    let root = Root::new();
    let publications = Publications::new();
    let before = children();
    let one = prepare(&root, &publications, "identity-one").unwrap();
    let two = prepare(&root, &publications, "identity-two").unwrap();
    assert_eq!(one.identity(), (100000, 100000));
    assert_eq!(two.identity(), (100001, 100001));
    verify_ids(&one);
    verify_ids(&two);
    assert_eq!(leases(), ["uid-100000", "uid-100001"]);
    let Err(busy) = prepare(&root, &publications, "identity-busy") else {
        panic!("live identity reused");
    };
    assert_eq!(busy.failure.stage, ExecutionStage::Identity);
    assert_eq!(busy.failure.errno, Some(11));
    assert!(busy.cleanup_failure.is_none());
    let output = Command::new("/kf/bin/identity-probe")
        .args(["fixed-probe", &two.root().init_pid().to_string()])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{:?} {:?}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let proof = String::from_utf8(output.stdout).unwrap();
    assert!(proof.contains("KF_IDENTITY_REDUCER_PROOF"));
    print!("{proof}");
    let result = one.refuse_remaining_isolation();
    assert_eq!(result.failure.stage, ExecutionStage::Seccomp);
    assert!(result.cleanup_failure.is_none());
    let reuse = prepare(&root, &publications, "identity-reuse").unwrap();
    assert_eq!(reuse.identity(), (100000, 100000));
    verify_ids(&reuse);
    drop(reuse);
    drop(two);
    assert!(leases().is_empty());
    assert_eq!(children(), before);
    for id in [
        "identity-one",
        "identity-two",
        "identity-busy",
        "identity-reuse",
    ] {
        assert!(!root.path.join(format!("kf-{id}")).exists());
        assert!(!Path::new(&format!("/var/lib/kf-judge/tasks/kf-{id}")).exists());
    }
    println!(
        "KF_IDENTITY_PROOF all_four_ids=1 groups=0 five_cap_sets=0 nnp=1 distinct_leases=1 exhaustion_refused=1 release_after_cleanup=1 safe_reuse=1 drop_cleanup=1 fixed_fixture_not_user_exec=1"
    );
}
#[test]
#[ignore = "requires independent capped VM"]
fn foreign_markers_and_unsafe_lease_roots_are_not_adopted() {
    let root = Root::new();
    let publications = Publications::new();
    let before = children();
    fs::create_dir_all(LEASES).unwrap();
    fs::set_permissions(LEASES, fs::Permissions::from_mode(0o700)).unwrap();
    let foreign = Path::new(LEASES).join("uid-100000");
    fs::create_dir(&foreign).unwrap();
    fs::set_permissions(&foreign, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(foreign.join("foreign"), b"must retain").unwrap();
    let target = prepare(&root, &publications, "identity-foreign").unwrap();
    assert_eq!(target.identity(), (100001, 100001));
    drop(target);
    assert_eq!(fs::read(foreign.join("foreign")).unwrap(), b"must retain");
    fs::remove_file(foreign.join("foreign")).unwrap();
    fs::remove_dir(&foreign).unwrap();
    fs::set_permissions(LEASES, fs::Permissions::from_mode(0o777)).unwrap();
    let Err(error) = prepare(&root, &publications, "identity-unsafe") else {
        panic!("writable lease root accepted");
    };
    assert_eq!(error.failure.stage, ExecutionStage::Identity);
    assert!(error.cleanup_failure.is_none());
    assert!(leases().is_empty());
    fs::set_permissions(LEASES, fs::Permissions::from_mode(0o700)).unwrap();
    fs::remove_file(publications.0.join("program-001")).unwrap();
    let Err(error) = prepare(&root, &publications, "identity-bad-input") else {
        panic!("missing input accepted");
    };
    assert_eq!(error.failure.stage, ExecutionStage::Preparation);
    assert!(error.cleanup_failure.is_none());
    assert!(leases().is_empty());
    assert_eq!(children(), before);
    println!(
        "KF_IDENTITY_LEASE_POLICY_PROOF foreign_retained=1 writable_root_refused=1 failed_preparation_slot_released=1 no_target_leftover=1"
    );
}
#[test]
#[ignore = "requires independent capped VM; cleanup failure injection"]
fn incomplete_target_cleanup_keeps_identity_reserved() {
    let root = Root::new();
    let publications = Publications::new();
    let before = children();
    let target = prepare(&root, &publications, "identity-cleanup-failure").unwrap();
    let task = Path::new("/var/lib/kf-judge/tasks/kf-identity-cleanup-failure");
    fs::write(task.join("foreign"), b"keep").unwrap();
    let error = target.refuse_remaining_isolation();
    assert_eq!(error.failure.stage, ExecutionStage::Seccomp);
    assert!(error.cleanup_failure.is_some());
    assert_eq!(leases(), ["uid-100000"]);
    assert!(!root.path.join("kf-identity-cleanup-failure").exists());
    assert_eq!(children(), before);
    let next = prepare(&root, &publications, "identity-after-failed-cleanup").unwrap();
    assert_eq!(next.identity(), (100001, 100001));
    drop(next);
    assert_eq!(fs::read(task.join("foreign")).unwrap(), b"keep");
    // VM operator restores the test baseline only after proving both task
    // cgroups/children gone. Fresh-process restart recovery is tested separately.
    fs::remove_file(task.join("foreign")).unwrap();
    fs::remove_dir(task).unwrap();
    fs::remove_file(Path::new(LEASES).join("uid-100000/owner.json")).unwrap();
    fs::remove_dir(Path::new(LEASES).join("uid-100000")).unwrap();
    assert!(leases().is_empty());
    println!(
        "KF_IDENTITY_CLEANUP_PROOF cleanup_error_separate=1 failed_slot_reserved=1 alternate_slot_only=1 foreign_not_deleted=1 test_baseline_operator_restore=1"
    );
}
#[test]
#[ignore = "requires independent capped VM with kernel setgroups failure injector"]
fn kernel_credential_failure_refuses_and_releases_own_lease() {
    let root = Root::new();
    assert_eq!(
        std::env::var("KF_SANDBOX_DENY_SETGROUPS").as_deref(),
        Ok("1")
    );
    let publications = Publications::new();
    let before = children();
    let Err(error) = prepare(&root, &publications, "identity-kernel-denied") else {
        panic!("credential error ignored");
    };
    assert_eq!(error.failure.stage, ExecutionStage::Identity);
    assert_eq!(error.failure.errno, Some(1));
    assert!(error.cleanup_failure.is_none());
    assert!(leases().is_empty());
    assert_eq!(children(), before);
    assert!(!root.path.join("kf-identity-kernel-denied").exists());
    assert!(!Path::new("/var/lib/kf-judge/tasks/kf-identity-kernel-denied").exists());
    println!(
        "KF_IDENTITY_FAILURE_PROOF real_kernel_eperm=1 identity_ack_refused=1 slot_released_after_teardown=1 child_reaped=1 task_cleanup=1"
    );
}
