//! Guarded fixed VM role applying the exact private production resource/reducer/filter code.
mod launcher {
    pub mod entry {
        pub use kf_sandbox::launcher::entry::ensure_single_threaded;
    }
}
mod spec {
    pub use kf_sandbox::spec::{ExecutionProfile, ValidatedSpec};
}
#[allow(dead_code)]
#[path = "../src/isolation/identity.rs"]
pub(crate) mod identity;
#[allow(dead_code)]
#[path = "../src/isolation/rlimits.rs"]
mod rlimits;
#[allow(dead_code)]
#[path = "../src/isolation/seccomp.rs"]
pub(crate) mod seccomp;
mod isolation {
    pub(crate) use crate::identity;
}
#[allow(dead_code)]
#[path = "../tests/support/mod.rs"]
mod support;
use std::os::unix::process::CommandExt;
fn main() {
    support::require_vm();
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(args.len(), 2);
    let (profile, path, mode) = match args[1].as_str() {
        "inherit" | "files" | "fds" | "stack" | "ceiling" => (
            spec::ExecutionProfile::Native,
            "/kf/bin/rlimit-inherit-probe",
            args[1].as_str(),
        ),
        "language-c" => (
            spec::ExecutionProfile::CompileC,
            "/kf/bin/seccomp-language-probe",
            "compile-c",
        ),
        "language-cpp" => (
            spec::ExecutionProfile::CompileCpp,
            "/kf/bin/seccomp-language-probe",
            "compile-cpp",
        ),
        "language-native" => (
            spec::ExecutionProfile::Native,
            "/kf/bin/seccomp-language-probe",
            "native",
        ),
        _ => panic!("fixed fixture mode"),
    };
    let mut request = support::spec("rlimit-fixture", 64 << 20, 8).spec().clone();
    request.profile = profile;
    request.limits = kf_sandbox::spec::ResourceLimits::for_profile(profile);
    if profile != spec::ExecutionProfile::Native {
        request.argv = vec![
            if profile == spec::ExecutionProfile::CompileC {
                "/usr/bin/gcc"
            } else {
                "/usr/bin/g++"
            }
            .into(),
            "/work/main".into(),
        ];
    }
    if !args[1].starts_with("language-") {
        request.limits.file_bytes = 4096;
        request.limits.stack_bytes = 262144;
        request.limits.open_files = 16;
    }
    let spec = request
        .validate(&kf_sandbox::spec::SpecPolicy::default())
        .unwrap();
    if args[1] == "ceiling" {
        use rustix::process::{Resource, Rlimit};
        let before = rustix::process::getrlimit(Resource::Fsize);
        rustix::process::setrlimit(
            Resource::Nofile,
            Rlimit {
                current: Some(15),
                maximum: Some(15),
            },
        )
        .unwrap();
        let e = rlimits::RlimitPlan::from_spec(&spec)
            .apply_current()
            .unwrap_err();
        assert_eq!(e.kind(), std::io::ErrorKind::PermissionDenied);
        assert_eq!(
            rustix::process::getrlimit(Resource::Nofile).maximum,
            Some(15)
        );
        assert_eq!(rustix::process::getrlimit(Resource::Fsize), before);
        println!(
            "KF_RLIMIT_CEILING_PROOF inherited_hard15_retained=1 raise_refused=1 preflight_no_partial_mutation=1"
        );
        return;
    }
    rlimits::RlimitPlan::from_spec(&spec)
        .apply_current()
        .unwrap();
    identity::reduce_current(100000, 100000).unwrap();
    seccomp::install(profile).unwrap();
    let mut command = std::process::Command::new(path);
    command.arg(mode).env_clear();
    if let Some(path) = profile.compiler_search_path() {
        command.env("PATH", path);
    }
    if args[1].starts_with("language-") {
        command.env("TMPDIR", "/kf/seccomp-work");
    }
    panic!("fixed fixture exec: {}", command.exec());
}
