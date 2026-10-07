//! Fixed guarded VM role using the exact private production reaper/reducer.
//! No production CLI user-exec gate or arbitrary fixture argv is added.
mod launcher {
    pub mod entry {
        pub use kf_sandbox::launcher::entry::ensure_single_threaded;
    }
}
mod io {
    pub use kf_sandbox::io::ChildPipes;
}
mod outcome {
    pub use kf_sandbox::outcome::Termination;
}
mod spec {
    pub use kf_sandbox::spec::{ExecutionProfile, ValidatedSpec};
}
#[allow(dead_code)]
#[path = "../src/isolation/identity.rs"]
pub(crate) mod identity;
#[allow(dead_code)]
#[path = "../src/isolation/namespaces.rs"]
mod namespaces;
#[allow(dead_code)]
#[path = "../src/isolation/process_tree.rs"]
mod process_tree;
#[allow(dead_code)]
#[path = "../src/isolation/seccomp.rs"]
pub(crate) mod seccomp;
mod isolation {
    pub(crate) use crate::{identity, seccomp};
}
#[allow(dead_code)]
#[path = "../tests/support/mod.rs"]
mod support;
use rustix::fd::{AsRawFd, OwnedFd};
use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

fn init(mode: &str) {
    assert_eq!(rustix::process::getpid(), rustix::process::Pid::INIT);
    rustix::process::set_parent_process_death_signal(Some(rustix::process::Signal::KILL)).unwrap();
    let mut control =
        UnixStream::from(rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stdin(), 3).unwrap());
    assert_eq!(control.as_raw_fd(), 3);
    control
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    control
        .set_write_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    // Original proc still shows outer PIDs. The fixed fresh PID 1 must replace
    // it before single-threaded/proc-based preconditions and credential checks.
    rustix::mount::mount(
        "proc",
        "/proc",
        "proc",
        rustix::mount::MountFlags::NOSUID
            | rustix::mount::MountFlags::NODEV
            | rustix::mount::MountFlags::NOEXEC,
        Some(c"hidepid=2,subset=pid"),
    )
    .unwrap();
    let (input, in_write) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC).unwrap();
    let (out_read, output) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC).unwrap();
    let (err_read, error) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC).unwrap();
    drop(in_write); // fixed empty stdin / EOF
    let mut request = support::spec("tree-fixture", 64 << 20, 8).spec().clone();
    request.argv = vec!["/work/main".into(), mode.into()];
    if mode == "exec-fail" {
        request.argv[0] = "/work/missing".into();
        request.inputs[0].path = "missing".into();
    }
    let plan = process_tree::ExecPlan::new(
        &request
            .validate(&kf_sandbox::spec::SpecPolicy::default())
            .unwrap(),
    )
    .unwrap();
    identity::reduce_current(100000, 100000).unwrap();
    seccomp::install(spec::ExecutionProfile::Native).unwrap();
    let mut reaper = process_tree::Reaper::spawn(
        plan,
        process_tree::ChildStdio {
            input,
            output,
            error,
        },
    )
    .unwrap();
    control.write_all(b"READY\n").unwrap();
    let mut main_reported = false;
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let observed = reaper.poll().unwrap();
        if let Some(status) = observed.main.as_ref().filter(|_| !main_reported) {
            assert_eq!(
                *status,
                outcome::Termination::Exited {
                    code: if mode == "exec-fail" { 126 } else { 7 }
                }
            );
            if mode == "exec-fail" {
                assert_eq!(
                    observed.exec,
                    process_tree::ExecObservation::Failed { errno: 2 }
                );
            }
            control.write_all(b"MAIN\n").unwrap();
            main_reported = true;
        }
        if observed.empty {
            assert!(main_reported);
            if mode == "orphan-short" {
                assert_eq!(observed.reaped_descendants, 1);
            }
            let mut out = std::fs::File::from(out_read);
            let mut err = std::fs::File::from(err_read);
            let mut output = Vec::new();
            let mut error = Vec::new();
            out.read_to_end(&mut output).unwrap();
            err.read_to_end(&mut error).unwrap();
            if mode != "exec-fail" {
                assert!(String::from_utf8_lossy(&output).contains("KF_TREE_USER_PROOF"));
            }
            if mode == "orphan-short" {
                assert!(String::from_utf8_lossy(&error).contains("KF_ORPHAN_ADOPTED_PROOF"));
            }
            control.write_all(b"DRAINED\n").unwrap();
            reaper.shutdown(process_tree::NamespaceEnd::Drained);
        }
        let mut poll = [rustix::event::PollFd::new(
            &control,
            rustix::event::PollFlags::IN,
        )];
        rustix::event::poll(
            &mut poll,
            Some(&rustix::event::Timespec {
                tv_sec: 0,
                tv_nsec: 2_000_000,
            }),
        )
        .unwrap();
        if !poll[0].revents().is_empty() {
            let mut byte = [0];
            // The fixed init uses the admitted read syscall; std UnixStream::read
            // uses recvfrom, which is deliberately outside this policy.
            match rustix::io::read(&control, &mut byte) {
                Ok(0) => reaper.shutdown(process_tree::NamespaceEnd::ControlLost),
                Ok(1) if byte[0] == b'C' => {
                    control.write_all(b"CANCELLED\n").unwrap();
                    reaper.shutdown(process_tree::NamespaceEnd::Cancelled);
                }
                _ => reaper.shutdown(process_tree::NamespaceEnd::MonitorFailed),
            }
        }
        if Instant::now() >= deadline {
            reaper.shutdown(process_tree::NamespaceEnd::MonitorFailed);
        }
    }
}
fn line(control: &mut UnixStream) -> String {
    let mut bytes = Vec::new();
    loop {
        let mut b = [0];
        control.read_exact(&mut b).unwrap();
        if b[0] == b'\n' {
            break;
        }
        bytes.push(b[0]);
        assert!(bytes.len() < 32);
    }
    String::from_utf8(bytes).unwrap()
}
fn main() {
    support::require_vm();
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(args.len(), 3);
    assert!(
        [
            "normal",
            "orphan-short",
            "orphan-cancel",
            "orphan-disconnect",
            "exec-fail"
        ]
        .contains(&args[2].as_str())
    );
    if args[1] == "init" {
        init(&args[2]);
        return;
    }
    assert_eq!(args[1], "fixed");
    launcher::entry::ensure_single_threaded().unwrap();
    let (mut parent, child) = UnixStream::pair().unwrap();
    parent
        .set_read_timeout(Some(Duration::from_secs(4)))
        .unwrap();
    parent
        .set_write_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    let own = std::env::current_exe().unwrap();
    namespaces::create().unwrap();
    let mut process = Command::new(own)
        .args(["init", &args[2]])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .stdin(Stdio::from(OwnedFd::from(child)))
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap();
    let pidfd = rustix::process::pidfd_open(
        rustix::process::Pid::from_raw(process.id() as i32).unwrap(),
        rustix::process::PidfdFlags::empty(),
    )
    .unwrap();
    assert_eq!(line(&mut parent), "READY");
    assert_eq!(line(&mut parent), "MAIN");
    if args[2] == "orphan-cancel" || args[2] == "orphan-disconnect" {
        assert!(
            process.try_wait().unwrap().is_none(),
            "main exit abandoned descendants"
        );
        if args[2] == "orphan-cancel" {
            parent.write_all(b"C").unwrap();
            assert_eq!(line(&mut parent), "CANCELLED");
        }
        drop(parent);
    } else {
        assert_eq!(line(&mut parent), "DRAINED");
        drop(parent);
    }
    let deadline = Instant::now() + Duration::from_secs(4);
    let status = loop {
        if let Some(s) = process.try_wait().unwrap() {
            break s;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    };
    if args[2] == "orphan-disconnect" {
        assert_eq!(status.code(), Some(125));
    } else {
        assert!(status.success(), "{status}");
    }
    let mut poll = [rustix::event::PollFd::new(
        &pidfd,
        rustix::event::PollFlags::IN,
    )];
    assert_eq!(
        rustix::event::poll(
            &mut poll,
            Some(&rustix::event::Timespec {
                tv_sec: 0,
                tv_nsec: 0
            })
        )
        .unwrap(),
        1
    );
    println!(
        "KF_TREE_PROOF mode={} pid1_reaper=1 main_pidfd=1 main_status_verified=1 init_pidfd_ready=1 namespace_cleanup=1",
        args[2]
    );
}
