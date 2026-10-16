//! Private per-attempt Unix control transport, never user stdin/stdout.
//! Messages cannot produce Lifecycle events or prove completed OS operations.

use crate::outcome::{ErrorCode, ExecutionStage};
use rustix::fd::{AsFd, OwnedFd};
use rustix::net::{
    RecvAncillaryBuffer, RecvAncillaryMessage, RecvFlags, ReturnFlags, SendAncillaryBuffer,
    SendAncillaryMessage, SendFlags,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::io::{self, IoSlice, IoSliceMut, Write};
use std::mem::MaybeUninit;
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::time::{Duration, Instant};

pub const MAX_MESSAGE_BYTES: usize = 1024;
const MAX_NAMESPACE_MESSAGE_BYTES: usize = crate::spec::MAX_SPEC_BYTES as usize + 128;
const FD_MARKER: u8 = 0xa5;
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
/// readiness claim. The final execution gate requires actual kernel acknowledgements.
#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub enum NamespaceCommand {
    Initialize {},
    PrepareRoot {
        work_bytes: u64,
        tmp_bytes: u64,
    },
    PrepareProcDev {},
    PrepareInputs {
        count: u32,
        bytes: u64,
    },
    AttachInput {
        path: String,
        bytes: u64,
        executable: bool,
    },
    FinishInputs {},
    PrepareRlimits {
        plan: crate::isolation::rlimits::RlimitPlan,
    },
    PrepareIdentity {
        uid: u32,
        gid: u32,
    },
    PrepareSeccomp {
        profile: crate::spec::ExecutionProfile,
    },
    PrepareExecution {
        request: Box<crate::spec::ExecutionSpec>,
    },
    ReleaseExecution {},
    Cancel {},
}

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum NamespaceEvent {
    Initialized {
        init_pid: u32,
    },
    RootInitialized {},
    ProcDevInitialized {},
    InputsStarted {},
    InputAttached {},
    InputsInitialized {},
    RlimitsInitialized {},
    IdentityInitialized {},
    SeccompInitialized {},
    ExecutionPrepared {},
    ExecutionSpawned {},
    Tree {
        record: super::execution::TreeRecord,
    },
    Failed {
        errno: Option<u16>,
    },
    Cancelled {},
}

pub(crate) mod sealed {
    pub trait Sealed {}
}

/// Sealed to keep the control protocol separate from arbitrary execution data.
pub trait WireMessage: sealed::Sealed + Serialize + DeserializeOwned {
    const MAX_BYTES: usize = MAX_MESSAGE_BYTES;
    fn valid(&self) -> bool;
}

impl sealed::Sealed for ParentMessage {}
impl sealed::Sealed for NamespaceCommand {}
impl WireMessage for NamespaceCommand {
    const MAX_BYTES: usize = MAX_NAMESPACE_MESSAGE_BYTES;
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
            Self::PrepareInputs { count, bytes } => {
                *count <= kf_contracts::judging_v2::MAX_CLASS_FILES as u32
                    && *bytes <= crate::artifacts::MAX_SNAPSHOT_BYTES
            }
            Self::AttachInput { path, bytes, .. } => {
                crate::spec::artifact_path(path, "input").is_ok()
                    && *bytes <= crate::artifacts::MAX_SNAPSHOT_BYTES
            }
            Self::PrepareIdentity { uid, gid } => crate::isolation::identity::valid(*uid, *gid),
            Self::PrepareRlimits { plan } => plan.valid(),
            Self::PrepareExecution { request } => request
                .clone()
                .validate(&crate::spec::SpecPolicy::default())
                .is_ok(),
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
    pending: Vec<u8>,
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
            pending: Vec::with_capacity(MAX_MESSAGE_BYTES + 8),
        })
    }

    pub(crate) fn execution_deadline(&mut self, wall_us: u64) -> io::Result<()> {
        self.runtime_deadline(wall_us, Duration::ZERO)
    }

    /// The ordinary RPC starts before catalog verification and isolation setup.
    /// Its fixed preparation allowance is separate from the student's measured
    /// execution wall limit. The factory's job/cancellation watcher stays active.
    pub(crate) fn request_deadline(&mut self, wall_us: u64) -> io::Result<()> {
        self.runtime_deadline(wall_us, Duration::from_secs(30))
    }

    fn runtime_deadline(&mut self, wall_us: u64, preparation: Duration) -> io::Result<()> {
        if wall_us == 0 || wall_us > 120_000_000 || self.poisoned || !self.pending.is_empty() {
            return Err(invalid("execution deadline"));
        }
        self.deadline =
            Instant::now() + preparation + Duration::from_micros(wall_us) + Duration::from_secs(5);
        Ok(())
    }

    /// Incremental receive of only the small runtime messages. Partial frames
    /// persist across ticks; descriptors and oversized lengths poison the stream.
    pub(crate) fn try_receive<T: WireMessage>(&mut self) -> io::Result<Option<T>> {
        let result = (|| {
            self.remaining()?;
            rustix::fs::fcntl_setfl(&self.stream, rustix::fs::OFlags::NONBLOCK)?;
            let received = (|| {
                let need = if self.pending.len() < 8 {
                    8
                } else {
                    8 + decode_header(self.pending[..8].try_into().unwrap(), MAX_MESSAGE_BYTES)?
                };
                if self.pending.len() < need {
                    let mut buffer = [0_u8; MAX_MESSAGE_BYTES];
                    let n = (need - self.pending.len()).min(buffer.len());
                    match self.read_message(&mut buffer[..n], false) {
                        Ok((count, _)) => self.pending.extend_from_slice(&buffer[..count]),
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => return Ok(None),
                        Err(e) => return Err(e),
                    }
                }
                if self.pending.len() < 8 {
                    return Ok(None);
                }
                let length =
                    decode_header(self.pending[..8].try_into().unwrap(), MAX_MESSAGE_BYTES)?;
                if self.pending.len() != length + 8 {
                    return Ok(None);
                }
                let value: T = serde_json::from_slice(&self.pending[8..])
                    .map_err(|_| invalid("runtime JSON"))?;
                if !value.valid() {
                    return Err(invalid("runtime message"));
                }
                self.pending.clear();
                Ok(Some(value))
            })();
            rustix::fs::fcntl_setfl(&self.stream, rustix::fs::OFlags::empty())?;
            received
        })();
        self.finish(result)
    }

    pub fn send<T: WireMessage>(&mut self, message: &T) -> io::Result<()> {
        let result = self.send_inner(message);
        self.finish(result)
    }

    pub fn receive<T: WireMessage>(&mut self) -> io::Result<T> {
        let result = self.receive_inner();
        self.finish(result)
    }

    /// Exactly one descriptor and one marker, immediately after AttachInput.
    /// SO_PEERCRED was authenticated before any capability can be received.
    pub(crate) fn send_fd(&mut self, fd: impl AsFd) -> io::Result<()> {
        let result = (|| {
            let mut space = [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(1))];
            let borrowed = [fd.as_fd()];
            let mut ancillary = SendAncillaryBuffer::new(&mut space);
            if !ancillary.push(SendAncillaryMessage::ScmRights(&borrowed)) {
                return Err(invalid("descriptor control buffer"));
            }
            loop {
                self.stream.set_write_timeout(Some(self.remaining()?))?;
                match rustix::net::sendmsg(
                    &self.stream,
                    &[IoSlice::new(&[FD_MARKER])],
                    &mut ancillary,
                    SendFlags::NOSIGNAL,
                ) {
                    Ok(1) => return Ok(()),
                    Err(error) if error == rustix::io::Errno::INTR => continue,
                    Err(error) => return Err(error.into()),
                    _ => return Err(invalid("descriptor marker write")),
                }
            }
        })();
        self.finish(result)
    }

    pub(crate) fn receive_fd(&mut self) -> io::Result<OwnedFd> {
        let result = (|| {
            let mut marker = [0_u8; 1];
            let (bytes, fd) = self.read_message(&mut marker, true)?;
            if bytes != 1 || marker[0] != FD_MARKER {
                return Err(invalid("descriptor marker read"));
            }
            fd.ok_or_else(|| invalid("missing input descriptor"))
        })();
        self.finish(result)
    }

    fn send_inner<T: WireMessage>(&mut self, message: &T) -> io::Result<()> {
        self.remaining()?;
        if !message.valid() {
            return Err(invalid("invalid control message"));
        }
        let mut body = FixedBuffer {
            limit: T::MAX_BYTES,
            ..FixedBuffer::default()
        };
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
        let length = decode_header(header, T::MAX_BYTES)?;
        let mut bytes = [0u8; MAX_NAMESPACE_MESSAGE_BYTES];
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
            let (count, _) = self.read_message(bytes, false)?;
            bytes = &mut bytes[count..];
        }
        Ok(())
    }

    fn read_message(
        &mut self,
        bytes: &mut [u8],
        allow_fd: bool,
    ) -> io::Result<(usize, Option<OwnedFd>)> {
        loop {
            self.stream.set_read_timeout(Some(self.remaining()?))?;
            let mut space = [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(2))];
            let mut ancillary = RecvAncillaryBuffer::new(&mut space);
            let received = match rustix::net::recvmsg(
                &self.stream,
                &mut [IoSliceMut::new(bytes)],
                &mut ancillary,
                RecvFlags::CMSG_CLOEXEC,
            ) {
                Err(error) if error == rustix::io::Errno::INTR => continue,
                other => other?,
            };
            let mut fd = None;
            let mut invalid_control = received.flags.contains(ReturnFlags::CTRUNC);
            for message in ancillary.drain() {
                match message {
                    RecvAncillaryMessage::ScmRights(fds) => {
                        for next in fds {
                            if !allow_fd || fd.is_some() {
                                invalid_control = true;
                            }
                            if fd.is_none() {
                                fd = Some(next);
                            }
                        }
                    }
                    _ => invalid_control = true,
                }
            }
            if invalid_control {
                return Err(invalid("unexpected or truncated descriptors"));
            }
            if received.bytes == 0 {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "control EOF"));
            }
            return Ok((received.bytes, fd));
        }
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

fn decode_header(header: [u8; 8], limit: usize) -> io::Result<usize> {
    if header[..4] != PREFIX {
        return Err(invalid("unsupported control framing"));
    }
    let length =
        u32::from_be_bytes(header[4..].try_into().expect("four-byte header length")) as usize;
    if length == 0 || length > limit {
        return Err(invalid("invalid control frame length"));
    }
    Ok(length)
}

struct FixedBuffer {
    bytes: [u8; MAX_NAMESPACE_MESSAGE_BYTES],
    len: usize,
    limit: usize,
}

impl Default for FixedBuffer {
    fn default() -> Self {
        Self {
            bytes: [0; MAX_NAMESPACE_MESSAGE_BYTES],
            len: 0,
            limit: MAX_MESSAGE_BYTES,
        }
    }
}

impl Write for FixedBuffer {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit - self.len {
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
    fn class_bindings_allow_flat_inner_names_and_a_bounded_complete_set() {
        for name in [
            "Main.class",
            "Main$1.class",
            "Main$Inner.class",
            "Helper.class",
        ] {
            assert!(
                NamespaceCommand::AttachInput {
                    path: name.into(),
                    bytes: 1,
                    executable: false,
                }
                .valid()
            );
        }
        for name in [
            "../Main.class",
            "pkg/Main$Inner.class",
            "Main$1.class/child",
        ] {
            assert!(
                !NamespaceCommand::AttachInput {
                    path: name.into(),
                    bytes: 1,
                    executable: false,
                }
                .valid()
            );
        }
        assert!(
            NamespaceCommand::PrepareInputs {
                count: 256,
                bytes: 1024
            }
            .valid()
        );
        assert!(
            !NamespaceCommand::PrepareInputs {
                count: 257,
                bytes: 1024
            }
            .valid()
        );
    }

    fn pair() -> (ControlChannel, ControlChannel) {
        let (a, b) = UnixStream::pair().unwrap();
        let peer = ExpectedPeer {
            pid: std::process::id(),
            uid: rustix::process::geteuid().as_raw(),
            gid: rustix::process::getegid().as_raw(),
        };
        (
            ControlChannel::new(a, peer, Instant::now() + MAX_CONTROL_TIMEOUT).unwrap(),
            ControlChannel::new(b, peer, Instant::now() + MAX_CONTROL_TIMEOUT).unwrap(),
        )
    }

    #[test]
    fn request_preparation_allowance_is_fixed_and_execution_limit_is_separate() {
        let (mut request, mut execution) = pair();
        let start = Instant::now();
        request.request_deadline(3_000_000).unwrap();
        execution.execution_deadline(3_000_000).unwrap();
        assert!(request.deadline.duration_since(start) >= Duration::from_secs(38));
        assert!(request.deadline.duration_since(start) < Duration::from_secs(39));
        assert!(execution.deadline.duration_since(start) >= Duration::from_secs(8));
        assert!(execution.deadline.duration_since(start) < Duration::from_secs(9));
        request.request_deadline(120_000_000).unwrap();
        assert!(request.deadline.duration_since(start) < Duration::from_secs(156));
        assert!(request.request_deadline(0).is_err());
        assert!(request.request_deadline(120_000_001).is_err());
        request.poisoned = true;
        assert!(request.request_deadline(3_000_000).is_err());
    }

    #[test]
    fn descriptor_transport_preserves_identity_and_cloexec() {
        let (mut a, mut b) = pair();
        let file = std::fs::File::open("/dev/null").unwrap();
        a.send_fd(&file).unwrap();
        let received = b.receive_fd().unwrap();
        let x = rustix::fs::fstat(&file).unwrap();
        let y = rustix::fs::fstat(&received).unwrap();
        assert_eq!((x.st_dev, x.st_ino), (y.st_dev, y.st_ino));
        assert!(
            rustix::io::fcntl_getfd(&received)
                .unwrap()
                .contains(rustix::io::FdFlags::CLOEXEC)
        );
    }

    #[test]
    fn descriptor_in_a_json_frame_poison_connection() {
        let (mut a, mut b) = pair();
        let file = std::fs::File::open("/dev/null").unwrap();
        a.send_fd(&file).unwrap();
        assert!(b.receive::<NamespaceCommand>().is_err());
        assert!(b.receive_fd().is_err());
        assert!(a.send(&NamespaceCommand::Cancel {}).is_err());
    }

    #[test]
    fn namespace_paths_fit_without_expanding_ordinary_control_frames() {
        let (mut a, mut b) = pair();
        let path = (0..10)
            .map(|_| "x".repeat(200))
            .collect::<Vec<_>>()
            .join("/");
        let command = NamespaceCommand::AttachInput {
            path,
            bytes: 0,
            executable: false,
        };
        a.send(&command).unwrap();
        assert_eq!(b.receive::<NamespaceCommand>().unwrap(), command);
        assert_eq!(ParentMessage::MAX_BYTES, 1024);
        assert_eq!(NamespaceCommand::MAX_BYTES, MAX_NAMESPACE_MESSAGE_BYTES);
    }

    #[test]
    fn header_rejects_oversized_length_before_body_read() {
        for length in [0, 1025, u32::MAX] {
            let mut header = [0u8; 8];
            header[..4].copy_from_slice(&PREFIX);
            header[4..].copy_from_slice(&length.to_be_bytes());
            assert!(decode_header(header, MAX_MESSAGE_BYTES).is_err());
        }
    }

    #[test]
    fn header_rejects_version_and_reserved_flags() {
        for prefix in [[b'K', b'F', 2, 0], [b'K', b'F', 1, 1], [0; 4]] {
            let mut header = [0u8; 8];
            header[..4].copy_from_slice(&prefix);
            header[4..].copy_from_slice(&1u32.to_be_bytes());
            assert!(decode_header(header, MAX_MESSAGE_BYTES).is_err());
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
    fn runtime_receive_preserves_partial_frame_and_rejects_fds() {
        let (mut a, mut b) = pair();
        let raw = serde_json::to_vec(&NamespaceCommand::Cancel {}).unwrap();
        let mut header = [0_u8; 8];
        header[..4].copy_from_slice(&PREFIX);
        header[4..].copy_from_slice(&(raw.len() as u32).to_be_bytes());
        a.stream.write_all(&header[..3]).unwrap();
        assert!(b.try_receive::<NamespaceCommand>().unwrap().is_none());
        a.stream.write_all(&header[3..]).unwrap();
        assert!(b.try_receive::<NamespaceCommand>().unwrap().is_none());
        a.stream.write_all(&raw[..2]).unwrap();
        assert!(b.try_receive::<NamespaceCommand>().unwrap().is_none());
        a.stream.write_all(&raw[2..]).unwrap();
        assert_eq!(
            b.try_receive::<NamespaceCommand>().unwrap(),
            Some(NamespaceCommand::Cancel {})
        );
        let file = std::fs::File::open("/dev/null").unwrap();
        a.send_fd(file).unwrap();
        assert!(b.try_receive::<NamespaceCommand>().is_err());
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
