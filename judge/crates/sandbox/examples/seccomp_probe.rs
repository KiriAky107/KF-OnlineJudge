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
    let profile = match args[1].as_str() {
        "native" => spec::ExecutionProfile::Native,
        "compile-c" => spec::ExecutionProfile::CompileC,
        "compile-cpp" => spec::ExecutionProfile::CompileCpp,
        _ => panic!("fixed fixture profile"),
    };
    isolation::identity::reduce_current(100000, 100000).unwrap();
    policy::install(profile).unwrap();
    let e = std::process::Command::new("/kf/bin/seccomp-inherit-probe")
        .arg(&args[1])
        .env_clear()
        .exec();
    panic!("fixed fixture exec: {e}");
}
