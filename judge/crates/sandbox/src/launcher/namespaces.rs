//! Fixed privileged initialization, with a fresh exec for namespace PID 1.
//! This stage never accepts request argv and never enables user execution.

use super::blocked::{InitializationFailure, PreparedTarget};
use super::protocol::{
    ControlChannel, ExpectedPeer, MAX_CONTROL_TIMEOUT, NamespaceCommand, NamespaceEvent,
};
use crate::outcome::ExecutionStage;
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use rustix::process::Signal;
use std::io;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub use crate::isolation::namespaces::NamespaceIds;

fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

fn inherited_channel(parent_pid: u32) -> io::Result<ControlChannel> {
    let fd = rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stdin(), 3)?;
    ControlChannel::new(
        UnixStream::from(fd),
        ExpectedPeer {
            pid: parent_pid,
            uid: rustix::process::geteuid().as_raw(),
            gid: rustix::process::getegid().as_raw(),
        },
        Instant::now() + MAX_CONTROL_TIMEOUT,
    )
}

/// Fixed namespace bootstrap. The inherited socket is authenticated before
/// unshare; changing PID namespaces changes SO_PEERCRED's PID translation.
pub fn bootstrap(expected_parent: u32) -> io::Result<()> {
    super::entry::ensure_single_threaded()?;
    rustix::process::set_parent_process_death_signal(Some(Signal::KILL))?;
    if expected_parent <= 1
        || rustix::process::getppid().map(|pid| pid.as_raw_pid() as u32) != Some(expected_parent)
    {
        return Err(denied("bootstrap parent mismatch"));
    }
    let mut channel = inherited_channel(expected_parent)?;
    rustix::process::kill_process(rustix::process::getpid(), Signal::STOP)?;
    match channel.receive::<NamespaceCommand>()? {
        NamespaceCommand::Cancel {} => {
            channel.send(&NamespaceEvent::Cancelled {})?;
            return Ok(());
        }
        NamespaceCommand::Initialize {} => {}
    }
    let setup = (|| {
        crate::isolation::namespaces::create()?;
        super::entry::ensure_single_threaded()?;
        // NEWPID applies to children, so a second fresh exec becomes PID 1.
        // No Rust initialization occurs in a custom fork/pre_exec callback.
        let child = Command::new(std::env::current_exe()?)
            .arg("namespace-init")
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .env_clear()
            .stdin(Stdio::from(channel.duplicate_stream()?))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        Ok::<_, io::Error>(child)
    })();
    let mut child = match setup {
        Ok(child) => child,
        Err(error) => {
            channel.send(&NamespaceEvent::Failed {
                errno: error.raw_os_error().and_then(|n| u16::try_from(n).ok()),
            })?;
            return Err(error);
        }
    };
    // PID 1 is the sole control writer now. Release the bootstrap's copies so
    // control EOF has its actual meaning. The bootstrap owns/reaps PID 1 only;
    // user descendants and the final init reaping loop are a separate stage.
    rustix::stdio::dup2_stdin(std::fs::File::open("/dev/null")?)?;
    drop(channel);
    let status = child.wait()?;
    if !status.success() {
        return Err(denied("namespace init refused execution"));
    }
    Ok(())
}

fn status_value(status: &str, name: &str) -> io::Result<u32> {
    status
        .lines()
        .find_map(|line| line.strip_prefix(name))
        .and_then(|raw| raw.trim().parse().ok())
        .ok_or_else(|| io::Error::other("missing process identity"))
}

/// This helper runs before a new proc mount: the inherited procfs still reports
/// outer PIDs, allowing the post-prctl parent check even when getppid() is zero
/// in the new PID namespace. Later root setup must replace this inherited proc.
pub fn namespace_init(expected_outer_parent: u32) -> io::Result<()> {
    super::entry::ensure_single_threaded()?;
    if rustix::process::getpid().as_raw_pid() != 1 || rustix::process::getppid().is_some() {
        return Err(denied("not the namespace init"));
    }
    rustix::process::set_parent_process_death_signal(Some(Signal::KILL))?;
    let status = std::fs::read_to_string("/proc/self/status")?;
    if expected_outer_parent <= 1 || status_value(&status, "PPid:")? != expected_outer_parent {
        return Err(denied("namespace init parent mismatch"));
    }
    // The inherited proc still shows the outer PID, but the task's cgroup
    // namespace must already be rooted at its own resource-limited group.
    if std::fs::read_to_string("/proc/self/cgroup")?.trim() != "0::/" {
        return Err(denied("cgroup namespace not rooted at the task"));
    }
    let fd = rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stdin(), 3)?;
    let mut channel = ControlChannel::inherited_from_authenticated_bootstrap(
        UnixStream::from(fd),
        Instant::now() + MAX_CONTROL_TIMEOUT,
    )?;
    channel.send(&NamespaceEvent::Initialized {
        init_pid: status_value(&status, "Pid:")?,
    })?;
    // Only the supervisor in the ancestor PID namespace stops PID 1. Signals
    // sent within the namespace have special PID-1 restrictions. While it
    // verifies/stops us, this bounded receive can only cancel/refuse.
    let _ = channel.receive::<NamespaceCommand>()?;
    Err(denied("namespace init has no execution permit"))
}

/// Actual namespaces only, not complete isolation or an execution permit.
/// Both helper processes remain in the same bounded, frozen task cgroup.
pub struct NamespacedTarget {
    prepared: PreparedTarget,
    init_pid: u32,
    ids: NamespaceIds,
}

impl NamespacedTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
    ) -> Result<Self, InitializationFailure> {
        let (mut prepared, mut control) =
            PreparedTarget::prepare_initializing(config, spec, executable)?;
        let initialize = (|| {
            let parent_ids = NamespaceIds::observe(std::process::id())?;
            control.send(&NamespaceCommand::Initialize {})?;
            prepared
                .cgroup
                .begin_namespace_initialization(&mut prepared.target)?;
            let init_pid = match control.receive::<NamespaceEvent>()? {
                NamespaceEvent::Initialized { init_pid } => init_pid,
                NamespaceEvent::Failed { errno: Some(errno) } => {
                    return Err(io::Error::from_raw_os_error(i32::from(errno)));
                }
                _ => return Err(denied("namespace initialization failed")),
            };
            let deadline = Instant::now() + Duration::from_secs(2);
            let pid = rustix::process::Pid::from_raw(init_pid as i32)
                .ok_or_else(|| denied("invalid namespace init PID"))?;
            let pidfd = rustix::process::pidfd_open(pid, rustix::process::PidfdFlags::empty())?;
            // The trusted bootstrap creates exactly one child and waits for
            // it. Verify its identity before signaling through the pidfd.
            let status = std::fs::read_to_string(format!("/proc/{init_pid}/status"))?;
            if status_value(&status, "PPid:")? != prepared.pid() {
                return Err(denied("namespace init parent mismatch"));
            }
            rustix::process::pidfd_send_signal(&pidfd, Signal::STOP)?;
            loop {
                let status = std::fs::read_to_string(format!("/proc/{init_pid}/status"))?;
                if status_value(&status, "PPid:")? != prepared.pid()
                    || !status
                        .lines()
                        .find_map(|line| line.strip_prefix("NSpid:"))
                        .is_some_and(|v| v.split_whitespace().last() == Some("1"))
                {
                    return Err(denied("namespace init identity mismatch"));
                }
                if status.lines().any(|line| line == "State:\tT (stopped)") {
                    break;
                }
                if Instant::now() >= deadline {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "namespace init stop deadline",
                    ));
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            let ids = NamespaceIds::observe(init_pid)?;
            ids.verify_separate(&parent_ids)?;
            prepared
                .cgroup
                .finish_namespace_initialization(&[prepared.pid(), init_pid])?;
            Ok::<_, io::Error>((init_pid, ids))
        })();
        match initialize {
            Ok((init_pid, ids)) => Ok(Self {
                prepared,
                init_pid,
                ids,
            }),
            Err(error) => Err(prepared.refuse_at(ExecutionStage::Namespaces, Some(&error))),
        }
    }

    pub fn init_pid(&self) -> u32 {
        self.init_pid
    }
    pub fn bootstrap_pid(&self) -> u32 {
        self.prepared.pid()
    }
    pub fn namespace_ids(&self) -> &NamespaceIds {
        &self.ids
    }
    pub fn cgroup(&self) -> &crate::cgroup::TaskCgroup {
        self.prepared.cgroup()
    }

    /// The next missing stage remains a hard refusal with actual group cleanup.
    pub fn refuse_unimplemented_root(mut self) -> InitializationFailure {
        self.prepared
            .refuse_at(ExecutionStage::RootFilesystem, None)
    }
}
