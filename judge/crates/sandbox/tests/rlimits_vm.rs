#![cfg(target_os = "linux")]
#[allow(dead_code)]
mod support;
use kf_sandbox::{
    artifacts::ArtifactRoot,
    launcher::seccomp::FilteredTarget,
    outcome::ExecutionStage,
    spec::{ExecutionProfile, ResourceLimits, SpecPolicy},
};
use std::os::unix::{fs::PermissionsExt, process::ExitStatusExt};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
struct Publications(PathBuf);
impl Publications {
    fn new() -> Self {
        support::require_vm();
        let p = PathBuf::from(format!("/kf/rlimits-publications-{}", std::process::id()));
        fs::create_dir(&p).unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(p.join("program-001"), b"fixed-non-task-payload").unwrap();
        fs::write(p.join("input-001"), b"fixed-private-input").unwrap();
        Self(p)
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
fn request(profile: ExecutionProfile) -> kf_sandbox::spec::ValidatedSpec {
    let mut s = support::spec("rlimit-production", 64 << 20, 8)
        .spec()
        .clone();
    s.profile = profile;
    s.limits = ResourceLimits::for_profile(profile);
    if profile != ExecutionProfile::Native {
        s.argv = vec![
            if profile == ExecutionProfile::CompileC {
                "/usr/bin/gcc"
            } else {
                "/usr/bin/g++"
            }
            .into(),
            "-c".into(),
            "/work/main.cpp".into(),
        ];
        s.outputs = vec!["main.o".into()];
    }
    s.validate(&SpecPolicy::default()).unwrap()
}
fn prepare(
    root: &support::Root,
    p: &Publications,
    s: &kf_sandbox::spec::ValidatedSpec,
) -> Result<FilteredTarget, kf_sandbox::launcher::blocked::InitializationFailure> {
    FilteredTarget::prepare(&root.validated(), s, &support::binary(), &p.root())
}
fn limits(text: &str, name: &str) -> Vec<String> {
    text.lines()
        .find_map(|l| l.strip_prefix(name))
        .unwrap()
        .split_whitespace()
        .map(String::from)
        .collect()
}
#[test]
#[ignore = "independent capped VM: production pre-credential resource stage and all profiles"]
fn production_init_applies_and_reads_back_finite_limits_before_identity_and_seccomp() {
    support::require_vm();
    let root = support::Root::new();
    let p = Publications::new();
    let before = support::children();
    for profile in [
        ExecutionProfile::Native,
        ExecutionProfile::CompileC,
        ExecutionProfile::CompileCpp,
    ] {
        let s = request(profile);
        let target = prepare(&root, &p, &s).unwrap();
        let text =
            fs::read_to_string(format!("/proc/{}/limits", target.root().init_pid())).unwrap();
        for (name, n, unit) in [
            ("Max file size", s.spec().limits.file_bytes, "bytes"),
            ("Max stack size", s.spec().limits.stack_bytes, "bytes"),
            ("Max open files", s.spec().limits.open_files.into(), "files"),
            ("Max core file size", 0, "bytes"),
        ] {
            assert_eq!(
                limits(&text, name),
                vec![n.to_string(), n.to_string(), unit.into()]
            );
        }
        let status =
            fs::read_to_string(format!("/proc/{}/status", target.root().init_pid())).unwrap();
        assert!(status.contains("NoNewPrivs:\t1\n") && status.contains("Seccomp:\t2\n"));
        assert_ne!(target.identity().0, 0);
        let e = target.refuse_remaining_isolation();
        assert_eq!(e.failure.stage, ExecutionStage::Exec);
        assert!(e.cleanup_failure.is_none());
        assert_eq!(support::children(), before);
        assert!(!root.path.join("kf-rlimit-production").exists());
        assert!(!Path::new("/var/lib/kf-judge/tasks/kf-rlimit-production").exists());
        assert_eq!(
            fs::read_dir("/var/lib/kf-judge/identities")
                .unwrap()
                .count(),
            0
        );
    }
    println!(
        "KF_RLIMIT_PRODUCTION_PROOF all_profiles=1 finite_soft_hard=1 core0=1 independent_readback=1 identity_seccomp=1 no_exec_permit=1 cleanup=1"
    );
}
#[test]
#[ignore = "independent capped VM: exact production setter then real fork/exec/thread and file/FD/stack limits"]
fn real_inheritance_file_fd_and_stack_limits() {
    support::require_vm();
    for mode in ["inherit", "files", "fds", "stack", "ceiling"] {
        let output = Command::new("/kf/bin/rlimit-probe")
            .arg(mode)
            .env_clear()
            .env("KF_SANDBOX_INDEPENDENT_VM", "1")
            .output()
            .unwrap();
        let stdout = String::from_utf8(output.stdout).unwrap();
        let stderr = String::from_utf8_lossy(&output.stderr);
        if mode == "ceiling" {
            assert!(output.status.success(), "{stdout} {stderr}");
            assert!(stdout.contains("KF_RLIMIT_CEILING_PROOF"));
            print!("{stdout}");
        } else if mode == "stack" {
            assert_eq!(output.status.signal(), Some(11), "{stdout} {stderr}");
            assert!(stdout.contains("KF_RLIMIT_STACK_READY"));
            println!("KF_RLIMIT_PROOF mode=stack sigsegv=1 core0=1 bounded128frames=1");
        } else {
            assert!(output.status.success(), "{mode} {stdout} {stderr}");
            assert!(stdout.contains(&format!("KF_RLIMIT_PROOF mode={mode}")));
            print!("{stdout}");
        }
    }
    assert_eq!(
        fs::metadata("/kf/rlimit-work/limited-file").unwrap().len(),
        4096
    );
    fs::remove_file("/kf/rlimit-work/limited-file").unwrap();
    assert_eq!(fs::read_dir("/kf/rlimit-work").unwrap().count(), 0);
}
#[test]
#[ignore = "independent capped VM: actual default RLIMIT with GNU C/C++ and dynamic native threads"]
fn profile_defaults_support_actual_gnu_and_native_dynamic_threaded_io() {
    support::require_vm();
    for (mode, profile) in [
        ("language-c", "compile-c"),
        ("language-cpp", "compile-cpp"),
        ("language-native", "native"),
    ] {
        let output = Command::new("/kf/bin/rlimit-probe")
            .arg(mode)
            .env_clear()
            .env("KF_SANDBOX_INDEPENDENT_VM", "1")
            .output()
            .unwrap();
        let stdout = String::from_utf8(output.stdout).unwrap();
        assert!(
            output.status.success(),
            "{stdout} {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(stdout.contains(&format!("KF_SECCOMP_LANGUAGE_PROOF profile={profile}")));
        print!("{stdout}");
    }
    println!(
        "KF_RLIMIT_LANGUAGE_PROOF default_profiles=1 real_gnu=1 dynamic_pthread_binary_empty_exit7=1"
    );
}
#[test]
#[ignore = "independent capped VM: real prlimit64 setter kernel EPERM injector"]
fn kernel_setter_failure_refuses_and_cleans_before_identity() {
    support::require_vm();
    assert_eq!(std::env::var("KF_SANDBOX_DENY_RLIMIT").as_deref(), Ok("1"));
    let root = support::Root::new();
    let p = Publications::new();
    let before = support::children();
    let e = match prepare(&root, &p, &request(ExecutionProfile::Native)) {
        Ok(_) => panic!("kernel RLIMIT failure accepted"),
        Err(e) => e,
    };
    assert_eq!(e.failure.stage, ExecutionStage::Preparation);
    assert_eq!(e.failure.errno, Some(1));
    assert!(e.cleanup_failure.is_none());
    assert_eq!(support::children(), before);
    assert!(!root.path.join("kf-rlimit-production").exists());
    assert!(!Path::new("/var/lib/kf-judge/tasks/kf-rlimit-production").exists());
    assert_eq!(
        fs::read_dir("/var/lib/kf-judge/identities")
            .unwrap()
            .count(),
        0
    );
    println!(
        "KF_RLIMIT_FAILURE_PROOF actual_kernel_eperm=1 pre_identity=1 ack_refused=1 owned_reaped=1 cg_root_lease_removed=1"
    );
}
