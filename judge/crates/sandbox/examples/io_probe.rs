//! Fixed guarded VM role using the exact private production reaper/reducer.
//! Exact production pipe pump and private reaper, guarded fixed VM-only fixture.
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
        .set_write_timeout(Some(Duration::from_secs(1)))
        .unwrap();
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
    let path = format!("/kf/io-capture-{}", mode);
    std::fs::create_dir(&path).unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    if mode == "storage-fail" {
        rustix::mount::mount(
            "tmpfs",
            &path,
            "tmpfs",
            rustix::mount::MountFlags::NOSUID
                | rustix::mount::MountFlags::NODEV
                | rustix::mount::MountFlags::NOEXEC,
            Some(c"size=8192,mode=0700"),
        )
        .unwrap();
    }
    let directory = rustix::fs::open(
        &path,
        rustix::fs::OFlags::PATH | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .unwrap();
    let artifacts = kf_sandbox::artifacts::ArtifactRoot::from_directory(
        rustix::fs::open(
            "/kf/io-publications",
            rustix::fs::OFlags::PATH | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )
        .unwrap(),
    )
    .unwrap();
    let mut request = support::spec("io-fixture", 64 << 20, 8).spec().clone();
    request.argv = vec!["/work/main".into(), mode.into()];
    if mode == "empty" {
        request.stdin_artifact = None;
    }
    if mode == "stdout-limit" {
        request.limits.stdout_bytes = 4096;
    }
    if mode == "stderr-limit" {
        request.limits.stderr_bytes = 4096;
    }
    let spec = request
        .validate(&kf_sandbox::spec::SpecPolicy::default())
        .unwrap();
    let data: Vec<_> = (0..262144).map(|i| (i % 251) as u8).collect();
    std::fs::write("/kf/io-publications/input-001", &data).unwrap();
    let input = artifacts.snapshot_inputs(&spec).unwrap().stdin;
    // Snapshot cursor/content do not follow publication changes.
    std::fs::write("/kf/io-publications/input-001", b"changed-after-sealing").unwrap();
    let (mut pump, child) = kf_sandbox::io::PipePump::prepare(input, &spec, directory).unwrap();
    child.validate_for_exec().unwrap();
    for fd in [&child.input, &child.output, &child.error] {
        assert!(
            !rustix::fs::fcntl_getfl(fd)
                .unwrap()
                .contains(rustix::fs::OFlags::NONBLOCK)
        );
    }
    assert_eq!(
        std::fs::read_dir(&path).unwrap().count(),
        0,
        "anonymous capture names leaked"
    );
    // The captures hold no directory inode; the private empty directory can be
    // removed before dropping identity and every anonymous inode dies on exit.
    if mode != "storage-fail" {
        std::fs::remove_dir(&path).unwrap();
    }
    drop(artifacts);
    let plan = process_tree::ExecPlan::new(&spec).unwrap();
    default_sigpipe();
    identity::reduce_current(100000, 100000).unwrap();
    seccomp::install(spec::ExecutionProfile::Native).unwrap();
    let mut reaper = process_tree::Reaper::spawn(plan, child).unwrap();
    control.write_all(b"READY\n").unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let output = match pump.poll() {
            Ok(s) => s,
            Err(e) => {
                assert_eq!(mode, "storage-fail", "unexpected pump error: {e}");
                assert_eq!(e.raw_os_error(), Some(libc::ENOSPC));
                let status = pump.status();
                assert!(!status.output_limit_exceeded());
                assert!(status.stdout.retained + status.stderr.retained <= 8192);
                assert!(
                    status.stdout.observed + status.stderr.observed
                        > status.stdout.retained + status.stderr.retained
                );
                pump.close_input();
                control.write_all(b"PROVED\n").unwrap();
                reaper.shutdown(process_tree::NamespaceEnd::MonitorFailed);
            }
        };
        let observed = reaper.poll().unwrap();
        if output.output_limit_exceeded() {
            let captured = if mode == "stdout-limit" {
                pump.stdout()
            } else {
                assert_eq!(mode, "stderr-limit");
                pump.stderr()
            };
            let status = captured.status();
            assert!(status.exceeded && !status.eof);
            assert_eq!(status.retained, 4096);
            assert_eq!(status.observed, 4097);
            let mut prefix = [0; 4096];
            assert_eq!(captured.read_at(&mut prefix, 0).unwrap(), 4096);
            assert!(prefix.iter().all(|b| *b == 0x5a));
            // At least one descendant remains; exiting PID1 must kill it even
            // when main already died of SIGPIPE after its output end closed.
            assert!(!observed.empty);
            pump.close_input();
            control.write_all(b"PROVED\n").unwrap();
            reaper.shutdown(process_tree::NamespaceEnd::Cancelled);
        }
        if observed.empty && output.outputs_eof() {
            assert_eq!(
                observed.main,
                Some(outcome::Termination::Exited { code: 7 })
            );
            assert_eq!(
                observed.exec,
                process_tree::ExecObservation::NoErrorObserved
            );
            assert!(!output.output_limit_exceeded() && output.stdin_closed);
            if mode == "duplex" {
                assert_eq!(output.stdin_sent, 262144);
                assert!(!output.stdin_closed_early);
                assert_capture(pump.stdout(), 262144, 0);
                assert_capture(pump.stderr(), 262144, 0xa5);
            } else if mode == "early-close" {
                assert!(output.stdin_closed_early && output.stdin_sent < 262144);
                let mut bytes = [0; 8];
                assert_eq!(pump.stdout().read_at(&mut bytes, 0).unwrap(), 8);
                assert_eq!(&bytes, b"CLOSED\0\xff");
                assert_eq!(output.stderr.observed, 0);
            } else {
                assert_eq!(mode, "empty");
                assert_eq!(output.stdin_sent, 0);
                assert_eq!(output.stdout.observed, 0);
                assert_eq!(output.stderr.observed, 0);
            }
            control.write_all(b"PROVED\n").unwrap();
            reaper.shutdown(process_tree::NamespaceEnd::Drained);
        }
        assert!(Instant::now() < deadline, "bounded I/O proof stalled");
        let mut poll = [rustix::event::PollFd::new(
            &control,
            rustix::event::PollFlags::IN,
        )];
        rustix::event::poll(
            &mut poll,
            Some(&rustix::event::Timespec {
                tv_sec: 0,
                tv_nsec: 100_000,
            }),
        )
        .unwrap();
        if !poll[0].revents().is_empty() {
            reaper.shutdown(process_tree::NamespaceEnd::ControlLost);
        }
    }
}
fn assert_capture(capture: &kf_sandbox::io::Capture, length: u64, xor: u8) {
    assert_eq!(capture.status().retained, length);
    assert_eq!(capture.status().observed, length);
    assert!(capture.status().eof);
    let mut chunk = [0; 4096];
    let mut offset = 0;
    while offset < length {
        let n = capture.read_at(&mut chunk, offset).unwrap();
        assert!(n > 0);
        for (i, b) in chunk[..n].iter().enumerate() {
            assert_eq!(*b, (((offset + i as u64) % 251) as u8) ^ xor);
        }
        offset += n as u64;
    }
    assert_eq!(capture.read_at(&mut chunk, length).unwrap(), 0);
}
#[allow(unsafe_code)]
fn default_sigpipe() {
    // SAFETY: fixed, single-threaded, guarded VM init before user fork. This
    // makes early-close exercise actual SIGPIPE containment, not Rust's IGN.
    unsafe {
        assert_ne!(libc::signal(libc::SIGPIPE, libc::SIG_DFL), libc::SIG_ERR);
    }
}
fn signal_mask_proof() {
    use std::os::unix::fs::PermissionsExt;
    launcher::entry::ensure_single_threaded().unwrap();
    assert_ne!(rustix::process::getpid(), rustix::process::Pid::INIT);
    let path = "/kf/io-signal-capture";
    std::fs::create_dir(path).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
    let directory = rustix::fs::open(
        path,
        rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .unwrap();
    let artifacts = kf_sandbox::artifacts::ArtifactRoot::from_directory(
        rustix::fs::open(
            "/kf/io-publications",
            rustix::fs::OFlags::PATH | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )
        .unwrap(),
    )
    .unwrap();
    let spec = support::spec("io-signal", 64 << 20, 8);
    let input = artifacts.snapshot_inputs(&spec).unwrap().stdin;
    let bad_directory = rustix::io::fcntl_dupfd_cloexec(&directory, 4).unwrap();
    let mut bad_spec = spec.spec().clone();
    bad_spec.limits.file_bytes = 4096;
    assert!(
        kf_sandbox::io::PipePump::prepare(
            None,
            &bad_spec
                .validate(&kf_sandbox::spec::SpecPolicy::default())
                .unwrap(),
            bad_directory
        )
        .is_err()
    );
    let (mut pump, mut child) = kf_sandbox::io::PipePump::prepare(input, &spec, directory).unwrap();
    child.validate_for_exec().unwrap();
    let alias = rustix::io::fcntl_dupfd_cloexec(&child.output, 4).unwrap();
    let original = std::mem::replace(&mut child.error, alias);
    assert!(child.validate_for_exec().is_err());
    child.error = original;
    rustix::fs::fcntl_setfl(&child.output, rustix::fs::OFlags::NONBLOCK).unwrap();
    assert!(child.validate_for_exec().is_err());
    rustix::fs::fcntl_setfl(&child.output, rustix::fs::OFlags::empty()).unwrap();
    std::mem::swap(&mut child.input, &mut child.output);
    assert!(child.validate_for_exec().is_err());
    std::mem::swap(&mut child.input, &mut child.output);
    child.validate_for_exec().unwrap();
    drop(child);
    default_sigpipe();
    let before = std::fs::read_to_string("/proc/self/status").unwrap();
    let observed = pump.poll().unwrap();
    assert!(observed.stdin_closed_early && observed.outputs_eof());
    let after = std::fs::read_to_string("/proc/self/status").unwrap();
    for field in ["SigBlk:", "SigPnd:", "ShdPnd:"] {
        assert_eq!(
            before.lines().find(|s| s.starts_with(field)),
            after.lines().find(|s| s.starts_with(field))
        );
    }
    drop(pump);
    assert_eq!(std::fs::read_dir(path).unwrap().count(), 0);
    std::fs::remove_dir(path).unwrap();
    println!(
        "KF_IO_SIGPIPE_PROOF default_disposition=1 ordinary_pid=1 early_close_survived=1 mask_restored=1 no_pending_leak=1 anonymous_cleanup=1 invalid_pipe_alias_direction_flags_and_budget_denied=1"
    );
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
            "duplex",
            "empty",
            "early-close",
            "stdout-limit",
            "stderr-limit",
            "storage-fail"
        ]
        .contains(&args[2].as_str())
    );
    if args[1] == "signal-mask" {
        signal_mask_proof();
        return;
    }
    if args[1] == "init" {
        init(&args[2]);
        return;
    }
    assert_eq!(args[1], "fixed");
    launcher::entry::ensure_single_threaded().unwrap();
    let (mut parent, child) = UnixStream::pair().unwrap();
    parent
        .set_read_timeout(Some(Duration::from_secs(12)))
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
    assert_eq!(line(&mut parent), "PROVED");
    drop(parent);
    let deadline = Instant::now() + Duration::from_secs(12);
    let status = loop {
        if let Some(s) = process.try_wait().unwrap() {
            break s;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    };
    if args[2] == "storage-fail" {
        assert_eq!(status.code(), Some(125));
        // The fixed outer wrapper shares this private mount namespace with
        // init; its own lifetime still pins the temporary capture mount.
        rustix::mount::unmount(
            "/kf/io-capture-storage-fail",
            rustix::mount::UnmountFlags::empty(),
        )
        .unwrap();
        std::fs::remove_dir("/kf/io-capture-storage-fail").unwrap();
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
        "KF_IO_PROOF mode={} child_fd012=1 snapshot_input=1 bounded_binary_capture=1 init_pidfd_ready=1 namespace_cleanup=1",
        args[2]
    );
}
