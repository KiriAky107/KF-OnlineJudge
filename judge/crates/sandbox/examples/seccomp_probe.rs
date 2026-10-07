//! VM-only fixed fixture, using the exact private production policy/reducer.
mod launcher {
    pub mod entry {
        pub use kf_sandbox::launcher::entry::ensure_single_threaded;
    }
}
mod spec {
    pub use kf_sandbox::spec::ExecutionProfile;
}
#[allow(dead_code)]
#[path = "../src/isolation/identity.rs"]
pub(crate) mod identity;
mod isolation {
    pub(crate) use crate::identity;
}
#[allow(dead_code)]
#[path = "../src/isolation/seccomp.rs"]
mod policy;
#[path = "../tests/support/mod.rs"]
#[allow(dead_code)]
mod support;
use std::os::unix::process::CommandExt;
fn main() {
    support::require_vm();
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(args.len(), 2);
    let (profile, fixture, mode) = match args[1].as_str() {
        "native" => (spec::ExecutionProfile::Native, "inherit", "native"),
        "compile-c" => (spec::ExecutionProfile::CompileC, "inherit", "compile-c"),
        "compile-cpp" => (spec::ExecutionProfile::CompileCpp, "inherit", "compile-cpp"),
        "danger-native" => (spec::ExecutionProfile::Native, "danger", "native"),
        "danger-c" => (spec::ExecutionProfile::CompileC, "danger", "compile-c"),
        "danger-cpp" => (spec::ExecutionProfile::CompileCpp, "danger", "compile-cpp"),
        "baseline" => (spec::ExecutionProfile::Native, "danger", "baseline"),
        "language-c" => (spec::ExecutionProfile::CompileC, "language", "compile-c"),
        "language-cpp" => (
            spec::ExecutionProfile::CompileCpp,
            "language",
            "compile-cpp",
        ),
        "language-native" => (spec::ExecutionProfile::Native, "language", "native"),
        _ => panic!("fixed fixture profile"),
    };
    isolation::identity::reduce_current(100000, 100000).unwrap();
    if mode != "baseline" {
        policy::install(profile).unwrap();
    }
    let path = match fixture {
        "inherit" => "/kf/bin/seccomp-inherit-probe",
        "danger" => "/kf/bin/seccomp-danger-probe",
        "language" => "/kf/bin/seccomp-language-probe",
        _ => unreachable!(),
    };
    let mut command = std::process::Command::new(path);
    command.arg(mode).env_clear();
    if let Some(path) = profile.compiler_search_path() {
        command.env("PATH", path);
    }
    if fixture == "language" {
        command.env("TMPDIR", "/kf/seccomp-work");
    }
    let e = command.exec();
    panic!("fixed fixture exec: {e}");
}
