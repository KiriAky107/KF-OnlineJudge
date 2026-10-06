#![cfg(target_os = "linux")]

use kf_sandbox::launcher::protocol::{ChildMessage, ControlChannel, ExpectedPeer, ParentMessage};
use kf_sandbox::outcome::{ErrorCode, ExecutionStage};
use std::fs::{self, DirBuilder};
use std::io::{self, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

static ATTEMPT: AtomicU64 = AtomicU64::new(0);

struct Attempt {
    directory: PathBuf,
    child: Option<Child>,
}

impl Attempt {
    fn start() -> (Self, ControlChannel, u32) {
        let directory = std::env::temp_dir().join(format!(
            "kf-l-{}-{}",
            std::process::id(),
            ATTEMPT.fetch_add(1, Ordering::Relaxed)
        ));
        DirBuilder::new().mode(0o700).create(&directory).unwrap();
        let mut attempt = Self {
            directory,
            child: None,
        };
        let socket = attempt.directory.join("control.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let child = Command::new(env!("CARGO_BIN_EXE_kf-sandbox"))
            .arg("launcher")
            .arg("--control-socket")
            .arg(&socket)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let pid = child.id();
        attempt.child = Some(child);
        let stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    assert!(
                        Instant::now() < deadline,
                        "launcher must connect within deadline"
                    );
                    assert!(
                        attempt
                            .child
                            .as_mut()
                            .unwrap()
                            .try_wait()
                            .unwrap()
                            .is_none()
                    );
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("cannot accept launcher: {error}"),
            }
        };
        let channel = ControlChannel::new(stream, own_peer(pid), deadline).unwrap();
        assert_eq!(
            fs::read_dir(format!("/proc/{pid}/task")).unwrap().count(),
            1
        );
        (attempt, channel, pid)
    }

    fn reap(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = self.child.as_mut().unwrap().try_wait().unwrap() {
                assert_eq!(
                    status.code(),
                    Some(2),
                    "entry never reports execution success"
                );
                self.child = None;
                return;
            }
            assert!(
                Instant::now() < deadline,
                "launcher must exit and be reaped"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

impl Drop for Attempt {
    fn drop(&mut self) {
        if let Some(child) = &mut self.child {
            let _ = child.kill();
            let _ = child.wait();
        }
        let _ = fs::remove_file(self.directory.join("control.sock"));
        let _ = fs::remove_dir(&self.directory);
    }
}

fn own_peer(pid: u32) -> ExpectedPeer {
    ExpectedPeer {
        pid,
        uid: rustix::process::geteuid().as_raw(),
        gid: rustix::process::getegid().as_raw(),
    }
}

fn pair() -> (ControlChannel, UnixStream) {
    let (stream, writer) = UnixStream::pair().unwrap();
    (
        ControlChannel::new(
            stream,
            own_peer(std::process::id()),
            Instant::now() + Duration::from_secs(2),
        )
        .unwrap(),
        writer,
    )
}

#[test]
fn fresh_single_thread_entry_exchanges_authenticated_messages_then_refuses() {
    let (mut attempt, mut channel, pid) = Attempt::start();
    assert_eq!(
        channel.receive::<ChildMessage>().unwrap(),
        ChildMessage::Hello { pid, threads: 1 }
    );
    channel
        .send(&ParentMessage::Initialize {
            run_id: "attempt-001".into(),
        })
        .unwrap();
    assert_eq!(
        channel.receive::<ChildMessage>().unwrap(),
        ChildMessage::InitializationRefused {
            run_id: "attempt-001".into(),
            stage: ExecutionStage::Preparation,
            code: ErrorCode::CapabilityUnavailable,
        }
    );
    attempt.reap();
    assert_eq!(
        channel.receive::<ChildMessage>().unwrap_err().kind(),
        io::ErrorKind::UnexpectedEof
    );
}

#[test]
fn parent_can_cancel_before_initialization_and_reap_entry() {
    let (mut attempt, mut channel, pid) = Attempt::start();
    assert_eq!(
        channel.receive::<ChildMessage>().unwrap(),
        ChildMessage::Hello { pid, threads: 1 }
    );
    channel.send(&ParentMessage::Cancel {}).unwrap();
    assert_eq!(
        channel.receive::<ChildMessage>().unwrap(),
        ChildMessage::Cancelled {}
    );
    attempt.reap();
}

#[test]
fn parent_disconnect_does_not_leave_a_waiting_launcher() {
    let (mut attempt, mut channel, _) = Attempt::start();
    assert!(matches!(
        channel.receive::<ChildMessage>().unwrap(),
        ChildMessage::Hello { .. }
    ));
    drop(channel);
    attempt.reap();
}

#[test]
fn kernel_credentials_reject_an_unexpected_peer() {
    for peer in [
        ExpectedPeer {
            pid: std::process::id() + 1,
            ..own_peer(std::process::id())
        },
        ExpectedPeer {
            uid: rustix::process::geteuid().as_raw() + 1,
            ..own_peer(std::process::id())
        },
        ExpectedPeer {
            gid: rustix::process::getegid().as_raw() + 1,
            ..own_peer(std::process::id())
        },
    ] {
        let (stream, _writer) = UnixStream::pair().unwrap();
        let result = ControlChannel::new(stream, peer, Instant::now() + Duration::from_secs(1));
        assert!(matches!(result, Err(error) if error.kind() == io::ErrorKind::PermissionDenied));
    }
}

#[test]
fn truncated_frame_poisoning_prevents_resynchronization() {
    let (mut channel, mut writer) = pair();
    writer.write_all(b"KF\x01").unwrap();
    drop(writer);
    assert_eq!(
        channel.receive::<ParentMessage>().unwrap_err().kind(),
        io::ErrorKind::UnexpectedEof
    );
    assert_eq!(
        channel.send(&ParentMessage::Cancel {}).unwrap_err().kind(),
        io::ErrorKind::BrokenPipe
    );
}

#[test]
fn two_frames_remain_separate_and_keep_their_direction() {
    let (mut channel, stream) = pair();
    let mut writer = ControlChannel::new(
        stream,
        own_peer(std::process::id()),
        Instant::now() + Duration::from_secs(2),
    )
    .unwrap();
    writer
        .send(&ParentMessage::Initialize {
            run_id: "run-1".into(),
        })
        .unwrap();
    writer.send(&ParentMessage::Cancel {}).unwrap();
    assert_eq!(
        channel.receive::<ParentMessage>().unwrap(),
        ParentMessage::Initialize {
            run_id: "run-1".into()
        }
    );
    assert_eq!(
        channel.receive::<ParentMessage>().unwrap(),
        ParentMessage::Cancel {}
    );
}

#[test]
fn unknown_fields_poison_channel_before_any_later_command() {
    let (mut channel, mut writer) = pair();
    let bytes = br#"{"command":"cancel","extra":true}"#;
    writer.write_all(b"KF\x01\x00").unwrap();
    writer
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .unwrap();
    writer.write_all(bytes).unwrap();
    assert_eq!(
        channel.receive::<ParentMessage>().unwrap_err().kind(),
        io::ErrorKind::InvalidData
    );
    assert_eq!(
        channel.receive::<ParentMessage>().unwrap_err().kind(),
        io::ErrorKind::BrokenPipe
    );
}

#[test]
fn partial_body_cannot_reset_the_absolute_deadline() {
    let (stream, mut writer) = UnixStream::pair().unwrap();
    let mut channel = ControlChannel::new(
        stream,
        own_peer(std::process::id()),
        Instant::now() + Duration::from_millis(80),
    )
    .unwrap();
    writer.write_all(b"KF\x01\x00\x00\x00\x00\x10{").unwrap();
    let started = Instant::now();
    let error = channel.receive::<ParentMessage>().unwrap_err();
    assert!(matches!(
        error.kind(),
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
    ));
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(
        channel.send(&ParentMessage::Cancel {}).unwrap_err().kind(),
        io::ErrorKind::BrokenPipe
    );
}

#[test]
fn deadline_must_be_finite_short_and_in_the_future() {
    for deadline in [
        Instant::now() - Duration::from_secs(1),
        Instant::now() + Duration::from_secs(30),
    ] {
        let (stream, _writer) = UnixStream::pair().unwrap();
        assert!(
            matches!(ControlChannel::new(stream, own_peer(std::process::id()), deadline),
            Err(error) if error.kind() == io::ErrorKind::InvalidInput)
        );
    }
}
