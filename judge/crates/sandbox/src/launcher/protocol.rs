//! Private per-attempt Unix control transport, never user stdin/stdout.
//! Messages cannot produce Lifecycle events or prove completed OS operations.

use crate::outcome::{ErrorCode, ExecutionStage};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::io::{self, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::time::{Duration, Instant};

pub const MAX_MESSAGE_BYTES: usize = 1024;
pub const MAX_CONTROL_TIMEOUT: Duration = Duration::from_secs(10);
const PREFIX: [u8; 4] = [b'K', b'F', 1, 0];

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum ParentMessage {
    Initialize { run_id: String },
    Cancel {},
}

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum ChildMessage {
    Hello {
        pid: u32,
        threads: u8,
    },
    InitializationRefused {
        run_id: String,
        stage: ExecutionStage,
        code: ErrorCode,
    },
    Cancelled {},
}

/// A distinct fixed initialization protocol. There is no argv, host path,
/// readiness claim or exec/release command in this protocol.
#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum NamespaceCommand {
    Initialize {},
    PrepareRoot { work_bytes: u64, tmp_bytes: u64 },
    PrepareProcDev {},
    Cancel {},
}

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum NamespaceEvent {
    Initialized { init_pid: u32 },
    RootInitialized {},
    ProcDevInitialized {},
    Failed { errno: Option<u16> },
    Cancelled {},
}

mod sealed {
    pub trait Sealed {}
}

/// Sealed to keep the control protocol separate from arbitrary execution data.
pub trait WireMessage: sealed::Sealed + Serialize + DeserializeOwned {
    fn valid(&self) -> bool;
}

impl sealed::Sealed for ParentMessage {}
impl sealed::Sealed for NamespaceCommand {}
impl WireMessage for NamespaceCommand {
    fn valid(&self) -> bool {
        match self {
            Self::PrepareRoot {
                work_bytes,
                tmp_bytes,
            } => crate::isolation::root::RootBudget {
                work_bytes: *work_bytes,
                tmp_bytes: *tmp_bytes,
            }
            .valid(),
            _ => true,
        }
    }
}
impl sealed::Sealed for NamespaceEvent {}
impl WireMessage for NamespaceEvent {
    fn valid(&self) -> bool {
        match self {
            Self::Initialized { init_pid } => *init_pid > 1,
            _ => true,
        }
    }
}
impl WireMessage for ParentMessage {
    fn valid(&self) -> bool {
        match self {
            Self::Initialize { run_id } => valid_run_id(run_id),
            Self::Cancel {} => true,
        }
    }
}

impl sealed::Sealed for ChildMessage {}
impl WireMessage for ChildMessage {
    fn valid(&self) -> bool {
        match self {
            Self::Hello { pid, threads } => *pid > 0 && *threads == 1,
            Self::InitializationRefused { run_id, .. } => valid_run_id(run_id),
            Self::Cancelled {} => true,
        }
    }
}

fn valid_run_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
}

/// Expected credentials come from the spawned Child handle / actual parent,
/// never the JSON payload. A separate stream is required for every attempt.
#[derive(Clone, Copy, Debug)]
pub struct ExpectedPeer {
    pub pid: u32,
    pub uid: u32,
    pub gid: u32,
}

pub struct ControlChannel {
    stream: UnixStream,
    deadline: Instant,
    poisoned: bool,
}

impl ControlChannel {
    pub(crate) fn duplicate_stream(&self) -> io::Result<rustix::fd::OwnedFd> {
        Ok(rustix::io::fcntl_dupfd_cloexec(&self.stream, 3)?)
    }
    pub fn new(stream: UnixStream, peer: ExpectedPeer, deadline: Instant) -> io::Result<Self> {
        let credentials = rustix::net::sockopt::socket_peercred(&stream)?;
        if credentials.pid.as_raw_pid() as u32 != peer.pid
            || credentials.uid.as_raw() != peer.uid
            || credentials.gid.as_raw() != peer.gid
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unexpected control peer",
            ));
        }
        Self::inherited_from_authenticated_bootstrap(stream, deadline)
    }

    /// Only the fresh namespace-init role uses this. Its endpoint was checked
    /// before unshare and exclusively inherited through the trusted Command.
    /// Do not call socket_peercred in PID 1: a peer outside its PID namespace
    /// has PID 0, which rustix UCred's nonzero Pid cannot represent.
    pub(crate) fn inherited_from_authenticated_bootstrap(
        stream: UnixStream,
        deadline: Instant,
    ) -> io::Result<Self> {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|duration| !duration.is_zero() && *duration <= MAX_CONTROL_TIMEOUT)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "invalid control deadline")
            })?;
        stream.set_nonblocking(false)?;
        stream.set_read_timeout(Some(remaining))?;
        stream.set_write_timeout(Some(remaining))?;
        Ok(Self {
            stream,
            deadline,
            poisoned: false,
        })
    }

    pub fn send<T: WireMessage>(&mut self, message: &T) -> io::Result<()> {
        let result = self.send_inner(message);
        self.finish(result)
    }

    pub fn receive<T: WireMessage>(&mut self) -> io::Result<T> {
        let result = self.receive_inner();
        self.finish(result)
    }

    fn send_inner<T: WireMessage>(&mut self, message: &T) -> io::Result<()> {
        self.remaining()?;
        if !message.valid() {
            return Err(invalid("invalid control message"));
        }
        let mut body = FixedBuffer::default();
        serde_json::to_writer(&mut body, message)
            .map_err(|_| invalid("control message too large"))?;
        let mut header = [0u8; 8];
        header[..4].copy_from_slice(&PREFIX);
        header[4..].copy_from_slice(&(body.len as u32).to_be_bytes());
        self.write_bytes(&header)?;
        self.write_bytes(&body.bytes[..body.len])
    }

    fn receive_inner<T: WireMessage>(&mut self) -> io::Result<T> {
        let mut header = [0u8; 8];
        self.read_bytes(&mut header)?;
        let length = decode_header(header)?;
        let mut bytes = [0u8; MAX_MESSAGE_BYTES];
        self.read_bytes(&mut bytes[..length])?;
        let message: T = serde_json::from_slice(&bytes[..length])
            .map_err(|_| invalid("invalid control JSON"))?;
        if !message.valid() {
            return Err(invalid("invalid control message"));
        }
        Ok(message)
    }

    fn remaining(&self) -> io::Result<Duration> {
        if self.poisoned {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "control channel failed",
            ));
        }
        self.deadline
            .checked_duration_since(Instant::now())
            .filter(|duration| !duration.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "control deadline expired"))
    }

    fn read_bytes(&mut self, mut bytes: &mut [u8]) -> io::Result<()> {
        while !bytes.is_empty() {
            self.stream.set_read_timeout(Some(self.remaining()?))?;
            match self.stream.read(bytes) {
                Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "control EOF")),
                Ok(count) => bytes = &mut bytes[count..],
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }

    fn write_bytes(&mut self, mut bytes: &[u8]) -> io::Result<()> {
        while !bytes.is_empty() {
            self.stream.set_write_timeout(Some(self.remaining()?))?;
            match self.stream.write(bytes) {
                Ok(0) => {
                    return Err(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "control write failed",
                    ));
                }
                Ok(count) => bytes = &bytes[count..],
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }

    fn finish<T>(&mut self, result: io::Result<T>) -> io::Result<T> {
        if result.is_err() {
            self.poisoned = true;
            let _ = self.stream.shutdown(Shutdown::Both);
        }
        result
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn decode_header(header: [u8; 8]) -> io::Result<usize> {
    if header[..4] != PREFIX {
        return Err(invalid("unsupported control framing"));
    }
    let length =
        u32::from_be_bytes(header[4..].try_into().expect("four-byte header length")) as usize;
    if length == 0 || length > MAX_MESSAGE_BYTES {
        return Err(invalid("invalid control frame length"));
    }
    Ok(length)
}

struct FixedBuffer {
    bytes: [u8; MAX_MESSAGE_BYTES],
    len: usize,
}

impl Default for FixedBuffer {
    fn default() -> Self {
        Self {
            bytes: [0; MAX_MESSAGE_BYTES],
            len: 0,
        }
    }
}

impl Write for FixedBuffer {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > MAX_MESSAGE_BYTES - self.len {
            return Err(invalid("control message too large"));
        }
        self.bytes[self.len..self.len + bytes.len()].copy_from_slice(bytes);
        self.len += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_rejects_oversized_length_before_body_read() {
        for length in [0, 1025, u32::MAX] {
            let mut header = [0u8; 8];
            header[..4].copy_from_slice(&PREFIX);
            header[4..].copy_from_slice(&length.to_be_bytes());
            assert!(decode_header(header).is_err());
        }
    }

    #[test]
    fn header_rejects_version_and_reserved_flags() {
        for prefix in [[b'K', b'F', 2, 0], [b'K', b'F', 1, 1], [0; 4]] {
            let mut header = [0u8; 8];
            header[..4].copy_from_slice(&prefix);
            header[4..].copy_from_slice(&1u32.to_be_bytes());
            assert!(decode_header(header).is_err());
        }
    }

    #[test]
    fn serializer_has_fixed_storage() {
        let mut buffer = FixedBuffer::default();
        buffer.write_all(&[0; MAX_MESSAGE_BYTES]).unwrap();
        assert!(buffer.write_all(b"extra").is_err());
        assert_eq!(buffer.len, MAX_MESSAGE_BYTES);
    }

    #[test]
    fn schemas_reject_unknown_fields_and_direction() {
        assert!(
            serde_json::from_str::<ParentMessage>(r#"{"command":"cancel","exec":true}"#).is_err()
        );
        assert!(serde_json::from_str::<ChildMessage>(r#"{"command":"cancel"}"#).is_err());
        assert!(serde_json::from_str::<ParentMessage>(r#"{"command":"release_exec"}"#).is_err());
        for raw in [
            r#"{"command":"initialize","argv":["/bin/sh"]}"#,
            r#"{"command":"release_exec"}"#,
            r#"{"command":"cancel","path":"/host"}"#,
        ] {
            assert!(serde_json::from_str::<NamespaceCommand>(raw).is_err());
        }
        assert!(
            serde_json::from_str::<NamespaceEvent>(
                r#"{"event":"initialized","init_pid":2,"exec_ready":true}"#
            )
            .is_err()
        );
    }

    #[test]
    fn identifiers_and_thread_claims_are_bounded() {
        for run_id in ["", "../work", "bad id"] {
            assert!(
                !ParentMessage::Initialize {
                    run_id: run_id.into()
                }
                .valid()
            );
        }
        assert!(
            !ParentMessage::Initialize {
                run_id: "x".repeat(65)
            }
            .valid()
        );
        assert!(!ChildMessage::Hello { pid: 1, threads: 2 }.valid());
        assert!(!ChildMessage::Hello { pid: 0, threads: 1 }.valid());
    }
}
