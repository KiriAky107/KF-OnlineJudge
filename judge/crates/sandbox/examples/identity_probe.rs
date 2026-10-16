//! Fixed VM-only fixture; compiles the exact private production reducer.
//! It supplies no production user execution capability or arbitrary command.
mod launcher {
    pub mod entry {
        pub use kf_sandbox::launcher::entry::ensure_single_threaded;
    }
}
#[allow(dead_code)]
#[path = "../src/isolation/identity.rs"]
mod identity;
#[path = "../tests/support/mod.rs"]
#[allow(dead_code)]
mod support;

fn main() {
    support::require_vm();
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(args.len(), 3);
    assert_eq!(args[1], "fixed-probe");
    let control = std::process::Command::new("/kf/bin/identity-inherit-probe")
        .arg("--suid-control-child")
        .env_clear()
        .output()
        .unwrap();
    assert!(
        control.status.success(),
        "{}",
        String::from_utf8_lossy(&control.stderr)
    );
    assert_eq!(
        control.stdout,
        b"KF_SUID_CONTROL_PROOF works_without_nnp=1\n"
    );
    print!("{}", String::from_utf8_lossy(&control.stdout));
    rustix::thread::set_thread_groups(&[rustix::process::Gid::from_raw(123)]).unwrap();
    let mut caps = rustix::thread::capabilities(None).unwrap();
    caps.inheritable |= rustix::thread::CapabilitySet::CHOWN;
    rustix::thread::set_capabilities(None, caps).unwrap();
    rustix::thread::configure_capability_in_ambient_set(rustix::thread::CapabilitySet::CHOWN, true)
        .unwrap();
    rustix::thread::set_keep_capabilities(true).unwrap();
    identity::reduce_current(100000, 100000).unwrap();
    for program in [
        "/kf/bin/identity-inherit-probe",
        "/kf/bin/identity-suid-probe",
    ] {
        let mut command = std::process::Command::new(program);
        command.env_clear();
        if let Some(pid) = args.get(2) {
            let pid: u32 = pid.parse().unwrap();
            assert!(pid > 1);
            command.arg(pid.to_string());
        }
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "{:?} {:?}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(output.stdout,b"KF_IDENTITY_INHERIT_PROBE ids=1 groups=0 caps=0 nnp=1 securebits=1 escalation_denied=1 fork_thread_inherit=1 peer_denied=1\n");
    }
    println!(
        "KF_IDENTITY_REDUCER_PROOF primed_inheritable_ambient_groups_keepcaps_cleared=1 setuid_exec_denied=1 fork_thread_inherit=1 peer_access_denied=1"
    );
}
