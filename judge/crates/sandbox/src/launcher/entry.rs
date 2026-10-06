//! Runs after a fresh exec of the trusted binary, without a Tokio runtime.
//! No custom pre_exec closure, fork, user program, or permission change occurs.

use super::protocol::{ChildMessage, ControlChannel, ExpectedPeer, ParentMessage};
use crate::outcome::{ErrorCode, ExecutionStage};
use std::io;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};

pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(5);

/// Parent must create a listener in a fresh private directory under its trusted
/// state root before spawning this entry. Socket path is trusted deployment data.
/// The kernel peer credentials, rather than that path, authenticate the parent.
pub fn serve(socket_path: &Path) -> io::Result<()> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    ensure_single_threaded()?;
    let parent = rustix::process::getppid()
        .filter(|pid| *pid != rustix::process::Pid::INIT)
        .ok_or_else(|| {
            io::Error::new(io::ErrorKind::PermissionDenied, "missing launcher parent")
        })?;
    // A full/stalled listener queue must fail, rather than block before the
    // channel deadline is installed. OwnedFd closes on every failure path.
    let socket = rustix::net::socket_with(
        rustix::net::AddressFamily::UNIX,
        rustix::net::SocketType::STREAM,
        rustix::net::SocketFlags::CLOEXEC | rustix::net::SocketFlags::NONBLOCK,
        None,
    )?;
    let address = rustix::net::SocketAddrUnix::new(socket_path)?;
    rustix::net::connect(&socket, &address)?;
    let stream = UnixStream::from(socket);
    let mut channel = ControlChannel::new(
        stream,
        ExpectedPeer {
            pid: parent.as_raw_pid() as u32,
            uid: rustix::process::geteuid().as_raw(),
            gid: rustix::process::getegid().as_raw(),
        },
        deadline,
    )?;
    channel.send(&ChildMessage::Hello {
        pid: std::process::id(),
        threads: 1,
    })?;
    match channel.receive::<ParentMessage>()? {
        ParentMessage::Initialize { run_id } => {
            // A transport handshake is never an isolation or exec permit.
            channel.send(&ChildMessage::InitializationRefused {
                run_id,
                stage: ExecutionStage::Preparation,
                code: ErrorCode::CapabilityUnavailable,
            })?;
        }
        ParentMessage::Cancel {} => channel.send(&ChildMessage::Cancelled {})?,
    }
    Ok(())
}

/// Must be repeated immediately before future privileged/fork operations.
pub fn ensure_single_threaded() -> io::Result<()> {
    let mut count = 0;
    for entry in std::fs::read_dir("/proc/self/task")? {
        entry?;
        count += 1;
        if count > 1 {
            break;
        }
    }
    if count != 1 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "launcher requires one thread",
        ));
    }
    Ok(())
}
