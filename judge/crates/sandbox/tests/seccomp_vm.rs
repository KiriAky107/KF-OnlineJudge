#![cfg(target_os = "linux")]
mod support;
use kf_sandbox::{
    artifacts::ArtifactRoot,
    launcher::seccomp::FilteredTarget,
    outcome::ExecutionStage,
    spec::{ExecutionProfile, SpecPolicy},
};
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::Command};

fn check(profile: ExecutionProfile, name: &str) {
    let root = support::Root::new();
    let before = support::children();
    let path = format!("/kf/seccomp-publications-{}", std::process::id());
    fs::create_dir(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(format!("{path}/program-001"), b"fixed-non-task-payload").unwrap();
    fs::write(format!("{path}/input-001"), b"private-stdin").unwrap();
    let artifacts = ArtifactRoot::from_directory(
        rustix::fs::open(
            &path,
            rustix::fs::OFlags::PATH | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )
        .unwrap(),
    )
    .unwrap();
    let mut spec = support::spec("seccomp-fixture", 64 << 20, 8).spec().clone();
    spec.run_id = format!("seccomp-{name}");
    spec.profile = profile;
    if profile != ExecutionProfile::Native {
        spec.argv = vec![
            if profile == ExecutionProfile::CompileC {
                "/usr/bin/gcc"
            } else {
                "/usr/bin/g++"
            }
            .into(),
            "/work/main".into(),
        ];
    }
    let result = FilteredTarget::prepare(
        &root.validated(),
        &spec.validate(&SpecPolicy::default()).unwrap(),
        &support::binary(),
        &artifacts,
    );
    if std::env::var("KF_SANDBOX_DENY_SECCOMP").as_deref() == Ok("1") {
        let Err(e) = result else {
            panic!("kernel policy failure ignored")
        };
        assert_eq!(e.failure.stage, ExecutionStage::Seccomp);
        assert_eq!(e.failure.errno, Some(1));
        assert!(e.cleanup_failure.is_none());
        println!("KF_SECCOMP_FAILURE_PROOF real_eperm=1 no_ack=1 target_reaped=1 lease_released=1");
    } else {
        let target = result.unwrap();
        let status =
            fs::read_to_string(format!("/proc/{}/status", target.root().init_pid())).unwrap();
        assert!(status.contains("Seccomp:\t2\n"));
        assert!(status.contains("Seccomp_filters:\t1\n"));
        assert!(status.contains("NoNewPrivs:\t1\n"));
        assert_eq!(target.identity(), (100000, 100000));
        assert!(target.root().snapshot_outputs().unwrap().is_empty());
        let e = target.refuse_remaining_isolation();
        assert_eq!(e.failure.stage, ExecutionStage::Exec);
        assert!(e.cleanup_failure.is_none());
        let output = Command::new("/kf/bin/seccomp-probe")
            .arg(name)
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
        print!("{}", String::from_utf8_lossy(&output.stdout));
        println!(
            "KF_SECCOMP_INIT_PROOF profile={name} frozen=1 filter_count=1 identity=1 run_refused=1"
        );
    }
    assert_eq!(support::children(), before);
    assert_eq!(
        fs::read_dir("/var/lib/kf-judge/identities")
            .unwrap()
            .count(),
        0
    );
    assert!(!Path::new(&format!("/var/lib/kf-judge/tasks/kf-seccomp-{name}")).exists());
    assert!(!root.path.join(format!("kf-seccomp-{name}")).exists());
    fs::remove_dir_all(path).unwrap();
}
#[test]
#[ignore = "requires independent capped VM"]
fn native_policy_installs_and_inherits() {
    check(ExecutionProfile::Native, "native");
}
#[test]
#[ignore = "requires independent capped VM"]
fn compile_c_policy_installs_and_inherits() {
    check(ExecutionProfile::CompileC, "compile-c");
}
#[test]
#[ignore = "requires independent capped VM"]
fn compile_cpp_policy_installs_and_inherits() {
    check(ExecutionProfile::CompileCpp, "compile-cpp");
}
#[test]
#[ignore = "requires independent capped VM with kernel seccomp failure injector"]
fn kernel_seccomp_failure_refuses_and_cleans() {
    assert_eq!(std::env::var("KF_SANDBOX_DENY_SECCOMP").as_deref(), Ok("1"));
    check(ExecutionProfile::Native, "native");
}
