//! Root service -> fresh single-threaded helper. Only a run identifier crosses
//! the allocation protocol. The accepted kernel-authenticated connection is the
//! capability, so the client never selects a helper path, executable or config.
use crate::{
    artifacts::ArtifactRoot, execution, launcher::protocol::WireMessage, rpc, spec::ExecutionSpec,
};
use rustix::{
    fd::OwnedFd,
    fs::{Mode, OFlags},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs::File,
    io::{self, Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
const MAX_IDS: usize = 4096;
fn denied() -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, "helper factory refused")
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Acquire {
    run_id: String,
}
#[derive(Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
enum Allocation {
    Ready { run_id: String, helper_pid: u32 },
    Refused {},
}
impl crate::launcher::protocol::sealed::Sealed for Acquire {}
impl crate::launcher::protocol::sealed::Sealed for Allocation {}
impl WireMessage for Acquire {
    fn valid(&self) -> bool {
        crate::spec::identifier(&self.run_id, "run_id").is_ok()
    }
}
impl WireMessage for Allocation {
    fn valid(&self) -> bool {
        match self {
            Self::Ready { run_id, helper_pid } => {
                *helper_pid > 1 && crate::spec::identifier(run_id, "run_id").is_ok()
            }
            Self::Refused {} => true,
        }
    }
}
pub struct FactoryClient {
    socket: PathBuf,
}
impl FactoryClient {
    pub fn new(socket: PathBuf) -> Self {
        Self { socket }
    }
    /// The caller holds only ordinary artifact and Unix socket capabilities.
    pub fn execute(
        &self,
        spec: ExecutionSpec,
        publication: &ArtifactRoot,
    ) -> io::Result<(rpc::Executed, u32)> {
        self.execute_controlled(spec, publication, None, None)
    }
    pub fn execute_controlled(
        &self,
        spec: ExecutionSpec,
        publication: &ArtifactRoot,
        deadline: Option<Instant>,
        cancellation: Option<&std::sync::atomic::AtomicBool>,
    ) -> io::Result<(rpc::Executed, u32)> {
        if rustix::process::geteuid().as_raw() < 1000 || rustix::process::getegid().as_raw() < 1000
        {
            return Err(denied());
        }
        let validated = spec
            .clone()
            .validate(&crate::spec::SpecPolicy::default())
            .map_err(|_| denied())?;
        let stream = UnixStream::connect(&self.socket)?;
        let interrupter = stream.try_clone()?;
        let done = std::sync::atomic::AtomicBool::new(false);
        std::thread::scope(|scope| {
            let watch = scope.spawn(|| {
                while !done.load(std::sync::atomic::Ordering::Acquire) {
                    if deadline.is_some_and(|d| Instant::now() >= d)
                        || cancellation
                            .is_some_and(|c| c.load(std::sync::atomic::Ordering::Acquire))
                    {
                        // Half close is an authenticated cancellation. Preserve the
                        // read side for the final report and sealed cleanup proof.
                        let _ = interrupter.shutdown(std::net::Shutdown::Write);
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(2));
                }
            });
            let result = (|| {
                let mut channel = rpc::channel(stream, 0, 0)?;
                channel.send(&Acquire {
                    run_id: spec.run_id.clone(),
                })?;
                let pid = match channel.receive::<Allocation>()? {
                    Allocation::Ready { run_id, helper_pid } if run_id == spec.run_id => helper_pid,
                    _ => return Err(denied()),
                };
                Ok((
                    rpc::execute_channel(channel, spec, publication, validated)?,
                    pid,
                ))
            })();
            done.store(true, std::sync::atomic::Ordering::Release);
            watch.join().map_err(|_| denied())?;
            result
        })
    }
}
struct SocketLease {
    path: PathBuf,
    dev: u64,
    ino: u64,
}
impl Drop for SocketLease {
    fn drop(&mut self) {
        if let Ok(m) = std::fs::symlink_metadata(&self.path)
            && m.dev() == self.dev
            && m.ino() == self.ino
        {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}
struct Active {
    child: Child,
    _guardian: UnixStream,
}
fn private_file(path: &Path) -> io::Result<File> {
    let fd = match rustix::fs::open(
        path,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(pin) => {
            let st = rustix::fs::fstat(&pin)?;
            if rustix::fs::FileType::from_raw_mode(st.st_mode) != rustix::fs::FileType::RegularFile
            {
                return Err(denied());
            }
            use rustix::fd::AsRawFd;
            rustix::fs::open(
                format!("/proc/self/fd/{}", pin.as_raw_fd()),
                OFlags::RDWR | OFlags::CLOEXEC,
                Mode::empty(),
            )?
        }
        Err(rustix::io::Errno::NOENT) => rustix::fs::open(
            path,
            OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )?,
        Err(e) => return Err(e.into()),
    };
    let s = rustix::fs::fstat(&fd)?;
    if s.st_uid != 0
        || s.st_gid != 0
        || s.st_mode & 0o7777 != 0o600
        || s.st_nlink != 1
        || rustix::fs::FileType::from_raw_mode(s.st_mode) != rustix::fs::FileType::RegularFile
    {
        return Err(denied());
    }
    Ok(File::from(fd))
}
fn idle_recovery(config: &crate::runtime::ValidatedRuntimeConfig) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let report = crate::cleanup::recover(config)?;
        if report
            .items
            .iter()
            .any(|r| !matches!(r.status, "active" | "reclaimed"))
        {
            return Err(denied());
        }
        if !report.items.iter().any(|r| r.status == "active") {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(denied());
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
/// Fixed executable/config come solely from the trusted service invocation.
/// Runtime is finite even when the service manager is misconfigured; cgroup
/// reservations are read back by authorize, and tasks retain their own gates.
pub fn serve(config_path: &Path, lifetime_seconds: u64) -> io::Result<()> {
    if !(1..=86400).contains(&lifetime_seconds) {
        return Err(denied());
    }
    // Root management receives no ambient business/credential environment.
    if std::env::vars_os().any(|(k, v)| {
        !matches!(
            (k.to_str(), v.to_str()),
            (Some("PATH"), Some("/usr/bin:/bin"))
                | (Some("LC_ALL" | "LANG"), Some("C" | "C.UTF-8"))
                | (Some("TZ"), Some("UTC"))
        )
    }) {
        return Err(denied());
    }
    let config = execution::load_config(config_path)?;
    execution::authorize(&config)?;
    let policy = config.config();
    if policy.aggregate.supervisor_processes < 4 + 8 * policy.max_concurrent_runs {
        return Err(denied());
    }
    let binary = std::env::current_exe()?;
    // Pin and validate the service binary; Worker cannot replace its ancestors.
    let parent = crate::isolation::root::open_directory(binary.parent().ok_or_else(denied)?)?;
    let fd = rustix::fs::openat(
        &parent,
        binary.file_name().ok_or_else(denied)?,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let st = rustix::fs::fstat(&fd)?;
    if st.st_uid != 0
        || st.st_gid != 0
        || st.st_mode & 0o7022 != 0
        || rustix::fs::FileType::from_raw_mode(st.st_mode) != rustix::fs::FileType::RegularFile
    {
        return Err(denied());
    }
    let root = Path::new(&policy.state_root).join("factory");
    if !root.exists() {
        std::fs::create_dir(&root)?;
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o711))?;
    }
    let root_fd = crate::isolation::root::open_directory(&root)?;
    let lock = private_file(&root.join("lock"))?;
    rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive)?;
    // Previous live children cancel through their guardian connection. Durable
    // task journals alone authorize recovery; foreign entries fail closed.
    idle_recovery(&config)?;
    let socket = root.join("control.sock");
    let record_path = root.join("socket.json");
    if let Ok(m) = std::fs::symlink_metadata(&socket) {
        let bytes = execution::read_bounded_file(&record_path, 1024)?;
        let record: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| denied())?;
        let trusted = std::fs::symlink_metadata(&record_path)?;
        if trusted.uid() != 0
            || trusted.gid() != 0
            || trusted.mode() & 0o7777 != 0o600
            || trusted.nlink() != 1
            || !trusted.is_file()
            || !m.file_type().is_socket()
            || m.uid() != 0
            || record != serde_json::json!({"dev":m.dev(),"ino":m.ino()})
        {
            return Err(denied());
        }
        std::fs::remove_file(&socket)?;
    }
    let mut ledger = private_file(&root.join("requests"))?;
    if ledger.metadata()?.len() > (MAX_IDS * 65) as u64 {
        return Err(denied());
    }
    let mut old = String::new();
    (&mut ledger)
        .take((MAX_IDS * 65 + 1) as u64)
        .read_to_string(&mut old)?;
    let mut ids = HashSet::new();
    for id in old.lines() {
        if crate::spec::identifier(id, "run_id").is_err() || !ids.insert(id.to_owned()) {
            return Err(denied());
        }
    }
    if ids.len() > MAX_IDS || (!old.is_empty() && !old.ends_with('\n')) {
        return Err(denied());
    }
    let listener = UnixListener::bind(&socket)?;
    let m = std::fs::symlink_metadata(&socket)?;
    let _lease = SocketLease {
        path: socket.clone(),
        dev: m.dev(),
        ino: m.ino(),
    };
    let record = serde_json::to_vec(&serde_json::json!({"dev":m.dev(),"ino":m.ino()}))?;
    let mut f = private_file(&record_path)?;
    f.set_len(0)?;
    f.write_all(&record)?;
    f.sync_all()?;
    rustix::fs::fsync(&root_fd)?;
    std::os::unix::fs::chown(&socket, Some(0), Some(policy.identities.worker_gid))?;
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o660))?;
    listener.set_nonblocking(true)?;
    // Actual overall task delegation limits, including concurrency and no swap.
    let _delegated = crate::cgroup::DelegatedCgroup::open(&config).map_err(|_| denied())?;
    let mut active: Vec<Active> = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(lifetime_seconds);
    let result = (|| {
        while Instant::now() < deadline {
            let mut i = 0;
            while i < active.len() {
                if let Some(status) = active[i].child.try_wait()? {
                    active.swap_remove(i);
                    // A failed helper may have durable residuals. Never start
                    // another task until recovery proves safe release.
                    if !status.success() {
                        idle_recovery(&config)?;
                    }
                } else {
                    i += 1;
                }
            }
            let stream = match listener.accept() {
                Ok((s, _)) => s,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(5));
                    continue;
                }
                Err(e) => return Err(e),
            };
            let peer = stream.try_clone()?;
            let Ok(mut channel) = rpc::channel(
                stream,
                policy.identities.worker_uid,
                policy.identities.worker_gid,
            ) else {
                continue;
            };
            let Ok(request) = channel.receive::<Acquire>() else {
                continue;
            };
            if active.len() >= policy.max_concurrent_runs as usize
                || ids.len() >= MAX_IDS
                || ids.contains(&request.run_id)
            {
                let _ = channel.send(&Allocation::Refused {});
                continue;
            }
            // Persist before allocating; IDs remain consumed across restarts.
            writeln!(ledger, "{}", request.run_id)?;
            ledger.sync_all()?;
            ids.insert(request.run_id.clone());
            let (guardian, child_guardian) = UnixStream::pair()?;
            let child = Command::new(&binary)
                .env_clear()
                .args(["factory-child", "--config"])
                .arg(config_path)
                .arg("--run-id")
                .arg(&request.run_id)
                .stdin(Stdio::from(OwnedFd::from(peer)))
                .stdout(Stdio::from(OwnedFd::from(child_guardian)))
                .stderr(Stdio::null())
                .spawn()?;
            let helper_pid = child.id();
            active.push(Active {
                child,
                _guardian: guardian,
            });
            if channel
                .send(&Allocation::Ready {
                    run_id: request.run_id,
                    helper_pid,
                })
                .is_err()
            {
                // Helper receives EOF, or times out within its fixed deadline.
            }
        }
        Ok(())
    })();
    drop(listener);
    // Guardian shutdown initiates the same bounded task-tree termination.
    for a in &active {
        let _ = a._guardian.shutdown(std::net::Shutdown::Both);
    }
    let stop_deadline = Instant::now() + Duration::from_secs(20);
    while !active.is_empty() && Instant::now() < stop_deadline {
        let mut i = 0;
        while i < active.len() {
            if active[i].child.try_wait()?.is_some() {
                active.swap_remove(i);
            } else {
                i += 1;
            }
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    if !active.is_empty() {
        return Err(denied());
    }
    idle_recovery(&config)?;
    result
}
/// Hidden child role: fixed accepted socket on stdin, factory lifeline on stdout.
/// No credentials, path selection, ambient stdio or previous task state survive.
pub fn child(config_path: &Path, run_id: &str) -> io::Result<()> {
    // Duplicate only the fixed descriptors installed by the root factory.
    let stream: UnixStream = rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stdin(), 3)?.into();
    let guardian = rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stdout(), 3)?;
    let peer = rustix::net::sockopt::socket_peercred(&guardian)?;
    if peer.uid.as_raw() != 0
        || peer.gid.as_raw() != 0
        || peer.pid != rustix::process::getppid().ok_or_else(denied)?
    {
        return Err(denied());
    }
    let config = execution::load_config(config_path)?;
    execution::authorize(&config)?;
    if crate::spec::identifier(run_id, "run_id").is_err() {
        return Err(denied());
    }
    let result = rpc::serve_stream(&config, stream, Some(run_id), Some(&guardian))?;
    if result["report"]["cleanup"] != "complete" {
        return Err(denied());
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protocol_never_accepts_worker_control_of_root_command_or_path() {
        for data in [
            r#"{"run_id":"a","argv":["/bin/sh"]}"#,
            r#"{"run_id":"a","config":"/tmp/x"}"#,
            r#"{"run_id":"../x"}"#,
            r#"{"run_id":""}"#,
        ] {
            let decoded = serde_json::from_str::<Acquire>(data);
            assert!(decoded.is_err() || !decoded.unwrap().valid());
        }
        assert!(
            serde_json::from_str::<Acquire>(r#"{"run_id":"a-1"}"#)
                .unwrap()
                .valid()
        );
    }
}
