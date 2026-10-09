//! One request per fresh privileged process. Kernel peer credentials authenticate
//! both endpoints; only the configured Worker may provide its own publication FD.
use crate::{
    artifacts::{ArtifactRoot, OutputSnapshot, SnapshotFile},
    execution,
    launcher::{
        protocol::{ControlChannel, ExpectedPeer, WireMessage},
        run,
    },
    outcome::{ExecutionError, ExecutionReport},
    spec::{ExecutionSpec, SpecPolicy},
};
use rustix::{
    fd::OwnedFd,
    fs::{Mode, OFlags},
};
use serde::{Deserialize, Serialize};
use std::{
    io,
    os::unix::{
        fs::{MetadataExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::Path,
    time::{Duration, Instant},
};
fn denied() -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, "sandbox RPC refused")
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    spec: ExecutionSpec,
}
#[derive(Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Reply {
    Completed {
        report: Box<ExecutionReport>,
    },
    Rejected {
        failure: ExecutionError,
        cleanup_failure: Option<ExecutionError>,
    },
}
impl crate::launcher::protocol::sealed::Sealed for Request {}
impl crate::launcher::protocol::sealed::Sealed for Reply {}
impl WireMessage for Request {
    const MAX_BYTES: usize = crate::spec::MAX_SPEC_BYTES as usize + 128;
    fn valid(&self) -> bool {
        self.spec.run_id.len() <= 64
    }
}
impl WireMessage for Reply {
    const MAX_BYTES: usize = crate::spec::MAX_SPEC_BYTES as usize + 128;
    fn valid(&self) -> bool {
        match self {
            Self::Completed { report } => {
                report.artifacts.len() <= kf_contracts::judging_v2::MAX_CLASS_FILES
                    && report.errors.len() <= 16
                    && report.limits.len() <= 8
            }
            Self::Rejected { .. } => true,
        }
    }
}
pub(crate) fn channel(stream: UnixStream, uid: u32, gid: u32) -> io::Result<ControlChannel> {
    let c = rustix::net::sockopt::socket_peercred(&stream)?;
    ControlChannel::new(
        stream,
        ExpectedPeer {
            pid: c.pid.as_raw_pid() as u32,
            uid,
            gid,
        },
        Instant::now() + Duration::from_secs(5),
    )
}
#[derive(Debug)]
pub struct Rejected {
    pub failure: ExecutionError,
    pub cleanup_failure: Option<ExecutionError>,
}
impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("sandbox request rejected")
    }
}
impl std::error::Error for Rejected {}
pub struct Executed {
    pub report: ExecutionReport,
    pub stdout: SnapshotFile,
    pub stderr: SnapshotFile,
    pub outputs: Vec<OutputSnapshot>,
}
/// No privileges are acquired: this endpoint requires an ordinary uid/gid.
pub fn execute(
    socket: &Path,
    spec: ExecutionSpec,
    publication: &ArtifactRoot,
) -> io::Result<Executed> {
    if rustix::process::geteuid().as_raw() < 1000 || rustix::process::getegid().as_raw() < 1000 {
        return Err(denied());
    }
    let validated = spec
        .clone()
        .validate(&SpecPolicy::default())
        .map_err(|_| denied())?;
    let c = channel(UnixStream::connect(socket)?, 0, 0)?;
    execute_channel(c, spec, publication, validated)
}
pub(crate) fn execute_channel(
    mut c: ControlChannel,
    spec: ExecutionSpec,
    publication: &ArtifactRoot,
    validated: crate::spec::ValidatedSpec,
) -> io::Result<Executed> {
    c.send(&Request { spec })?;
    c.send_fd(publication)?;
    c.execution_deadline(validated.spec().limits.wall_time_us)?;
    let report = match c.receive::<Reply>()? {
        Reply::Completed { report } => *report,
        Reply::Rejected {
            failure,
            cleanup_failure,
        } => {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                Rejected {
                    failure,
                    cleanup_failure,
                },
            ));
        }
    };
    report.classify(&validated).map_err(|_| denied())?;
    let stdout = SnapshotFile::from_received(c.receive_fd()?, report.stdout.retained_bytes)?;
    let stderr = SnapshotFile::from_received(c.receive_fd()?, report.stderr.retained_bytes)?;
    let mut outputs = Vec::new();
    for a in &report.artifacts {
        outputs.push(OutputSnapshot {
            path: a.path.clone(),
            file: SnapshotFile::from_received(c.receive_fd()?, a.bytes)?,
        });
    }
    Ok(Executed {
        report,
        stdout,
        stderr,
        outputs,
    })
}
struct SocketLease<'a> {
    parent: OwnedFd,
    name: &'a std::ffi::OsStr,
    dev: u64,
    ino: u64,
}
impl Drop for SocketLease<'_> {
    fn drop(&mut self) {
        if let Ok(s) = rustix::fs::statat(
            &self.parent,
            self.name,
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        ) && s.st_dev == self.dev
            && s.st_ino == self.ino
        {
            let _ = rustix::fs::unlinkat(&self.parent, self.name, rustix::fs::AtFlags::empty());
        }
    }
}
/// Operator supplies protected config/socket paths; a service manager provisions
/// the finite helper scope. No internal sudo, account changes or deployment.
pub fn serve_one(config_path: &Path, socket: &Path) -> io::Result<serde_json::Value> {
    let config = execution::load_config(config_path)?;
    execution::authorize_registry(&config)?;
    let parent = crate::isolation::root::open_directory(socket.parent().ok_or_else(denied)?)?;
    let name = socket.file_name().ok_or_else(denied)?;
    // Refuse replacement, including an existing stale/foreign socket.
    if rustix::fs::statat(&parent, name, rustix::fs::AtFlags::SYMLINK_NOFOLLOW).is_ok() {
        return Err(denied());
    }
    let listener = UnixListener::bind(socket)?;
    let m = std::fs::symlink_metadata(socket)?;
    let lease = SocketLease {
        parent,
        name,
        dev: m.dev(),
        ino: m.ino(),
    };
    let socket_fd = rustix::fs::openat(
        &lease.parent,
        name,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let stat = rustix::fs::fstat(&socket_fd)?;
    if stat.st_dev != lease.dev || stat.st_ino != lease.ino || stat.st_uid != 0 {
        return Err(denied());
    }
    // Ancestors are non-writable to Worker. Operations cannot follow a substituted path.
    std::os::unix::fs::chown(socket, Some(0), Some(config.config().identities.worker_gid))?;
    std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o660))?;
    listener.set_nonblocking(true)?;
    let deadline = Instant::now() + Duration::from_secs(5);
    let stream = loop {
        match listener.accept() {
            Ok((s, _)) => break s,
            Err(e) if e.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(5))
            }
            Err(e) => return Err(e),
        }
    };
    drop(listener);
    serve_stream(&config, stream, None, None)
}
pub(crate) fn serve_stream(
    config: &crate::runtime::ValidatedRuntimeConfig,
    stream: UnixStream,
    binding: Option<&str>,
    guardian: Option<&OwnedFd>,
) -> io::Result<serde_json::Value> {
    let ids = &config.config().identities;
    let mut c = channel(stream, ids.worker_uid, ids.worker_gid)?;
    let request = c.receive::<Request>()?;
    if binding.is_some_and(|id| id != request.spec.run_id) {
        return Err(denied());
    }
    let artifacts = ArtifactRoot::from_worker_directory(c.receive_fd()?, ids.worker_uid)?;
    let peer = c.duplicate_stream()?;
    c.execution_deadline(request.spec.limits.wall_time_us)?;
    match execution::execute(
        config,
        request.spec,
        &artifacts,
        guardian.map_or(run::MonitorMode::Peer(&peer), |g| {
            run::MonitorMode::FactoryPeers(&peer, g)
        }),
    ) {
        Ok((result, _)) => {
            let report = result.report;
            // Cleanup failures remain in the report; caller may never claim a successful verdict.
            if report.cleanup != crate::outcome::CleanupState::Complete {
                return Err(denied());
            }
            let sent = c.send(&Reply::Completed {
                report: Box::new(report.clone()),
            });
            if sent.is_err() && report.cancelled_at_wall_time_us.is_some() {
                return Ok(serde_json::json!({"report":report,"proof":result.proof}));
            }
            sent?;
            c.send_fd(&result.stdout)?;
            c.send_fd(&result.stderr)?;
            for o in result.outputs {
                c.send_fd(&o.file)?;
            }
            Ok(serde_json::json!({"report":report,"proof":result.proof}))
        }
        Err(e) => {
            let failure = e.failure.ok_or_else(denied)?;
            c.send(&Reply::Rejected {
                failure,
                cleanup_failure: e.cleanup_failure,
            })?;
            Err(denied())
        }
    }
}
