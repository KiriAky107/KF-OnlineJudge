//! Fixed privileged initialization, with a fresh exec for namespace PID 1.
//! This stage never accepts request argv and never enables user execution.

use super::blocked::{InitializationFailure, PreparedTarget};
use super::protocol::{
    ControlChannel, ExpectedPeer, MAX_CONTROL_TIMEOUT, NamespaceCommand, NamespaceEvent,
};
use crate::outcome::ExecutionStage;
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use rustix::fd::OwnedFd;
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
pub fn bootstrap(expected_parent: u32, initialize_root: bool) -> io::Result<()> {
    super::entry::ensure_single_threaded()?;
    super::entry::ensure_inherited_descriptors()?;
    #[cfg(feature = "vm-acceptance")]
    crate::acceptance::verify_guest(false)?;
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
        _ => {
            return Err(denied("root command before namespaces"));
        }
    }
    let setup = (|| {
        crate::isolation::namespaces::create()?;
        super::entry::ensure_single_threaded()?;
        // NEWPID applies to children, so a second fresh exec becomes PID 1.
        // No Rust initialization occurs in a custom fork/pre_exec callback.
        let mut command = Command::new(std::env::current_exe()?);
        command
            .arg("namespace-init")
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .env_clear()
            .stdin(Stdio::from(channel.duplicate_stream()?))
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if initialize_root {
            command
                .arg("--initialize-root")
                .stdout(Stdio::from(rustix::io::fcntl_dupfd_cloexec(
                    rustix::stdio::stdout(),
                    3,
                )?))
                .stderr(Stdio::from(rustix::io::fcntl_dupfd_cloexec(
                    rustix::stdio::stderr(),
                    3,
                )?))
                .current_dir("/");
        }
        let child = command.spawn()?;
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
    let null = std::fs::File::options()
        .read(true)
        .write(true)
        .open("/dev/null")?;
    rustix::stdio::dup2_stdin(&null)?;
    if initialize_root {
        rustix::stdio::dup2_stdout(&null)?;
        rustix::stdio::dup2_stderr(&null)?;
    }
    drop(null);
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
pub fn namespace_init(expected_outer_parent: u32, initialize_root: bool) -> io::Result<()> {
    super::entry::ensure_single_threaded()?;
    super::entry::ensure_inherited_descriptors()?;
    #[cfg(feature = "vm-acceptance")]
    crate::acceptance::verify_guest(false)?;
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
    match channel.receive::<NamespaceCommand>()? {
        NamespaceCommand::PrepareRoot {
            work_bytes,
            tmp_bytes,
        } if initialize_root => {
            if let Err(error) =
                crate::isolation::root::install(crate::isolation::root::RootBudget {
                    work_bytes,
                    tmp_bytes,
                })
            {
                channel.send(&NamespaceEvent::Failed {
                    errno: error.raw_os_error().and_then(|n| u16::try_from(n).ok()),
                })?;
                return Err(error);
            }
            channel.send(&NamespaceEvent::RootInitialized {})?;
            match channel.receive::<NamespaceCommand>()? {
                NamespaceCommand::PrepareProcDev {} => {
                    if let Err(error) = crate::isolation::proc_dev::install() {
                        channel.send(&NamespaceEvent::Failed {
                            errno: error.raw_os_error().and_then(|n| u16::try_from(n).ok()),
                        })?;
                        return Err(error);
                    }
                    channel.send(&NamespaceEvent::ProcDevInitialized {})?;
                    match channel.receive::<NamespaceCommand>()? {
                        NamespaceCommand::PrepareInputs { count, bytes } => {
                            super::inputs::initialize(&mut channel, count, bytes)?;
                        }
                        NamespaceCommand::Cancel {} => return Ok(()),
                        _ => return Err(denied("input command before preparation")),
                    }
                    match channel.receive::<NamespaceCommand>()? {
                        NamespaceCommand::PrepareRlimits { plan } => {
                            let result = plan.apply_init();
                            if let Err(e) = result {
                                let _ = channel.send(&NamespaceEvent::Failed {
                                    errno: e.raw_os_error().and_then(|n| u16::try_from(n).ok()),
                                });
                                return Err(e);
                            }
                            channel.send(&NamespaceEvent::RlimitsInitialized {})?;
                        }
                        NamespaceCommand::Cancel {} => return Ok(()),
                        _ => return Err(denied("rlimit command before inputs")),
                    }
                    match channel.receive::<NamespaceCommand>()? {
                        NamespaceCommand::PrepareIdentity { uid, gid } => {
                            super::identity::initialize(&mut channel, uid, gid)?;
                            match channel.receive::<NamespaceCommand>()? {
                                NamespaceCommand::PrepareSeccomp { profile } => {
                                    super::seccomp::initialize(&mut channel, profile)?;
                                    match channel.receive::<NamespaceCommand>()? {
                                        #[cfg(feature = "vm-acceptance")]
                                        NamespaceCommand::PrepareExecution { request } => {
                                            return super::execution::serve(&mut channel, *request);
                                        }
                                        NamespaceCommand::Cancel {} => return Ok(()),
                                        _ => return Err(denied("execution gate unavailable")),
                                    }
                                }
                                NamespaceCommand::Cancel {} => return Ok(()),
                                _ => return Err(denied("seccomp command before identity")),
                            }
                        }
                        NamespaceCommand::Cancel {} => return Ok(()),
                        _ => return Err(denied("identity command before inputs")),
                    }
                }
                NamespaceCommand::Cancel {} => return Ok(()),
                _ => {}
            }
        }
        NamespaceCommand::Cancel {} => return Ok(()),
        _ => {}
    }
    Err(denied("namespace init has no execution permit"))
}

/// Actual namespaces only, not complete isolation or an execution permit.
/// Both helper processes remain in the same bounded, frozen task cgroup.
pub struct NamespacedTarget {
    pub(crate) prepared: PreparedTarget,
    pub(crate) control: ControlChannel,
    pub(crate) init_pidfd: OwnedFd,
    allow_root: bool,
    root_ready: bool,
    proc_dev_ready: bool,
    inputs_ready: bool,
    rlimits_ready: bool,
    identity_ready: bool,
    seccomp_ready: bool,
    init_pid: u32,
    ids: NamespaceIds,
}

impl NamespacedTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
    ) -> Result<Self, InitializationFailure> {
        Self::prepare_inner(config, spec, executable, None)
    }

    pub(crate) fn prepare_for_root(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
        roots: crate::isolation::root::RootDescriptors,
    ) -> Result<Self, InitializationFailure> {
        Self::prepare_inner(config, spec, executable, Some(roots))
    }
    fn prepare_inner(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
        roots: Option<crate::isolation::root::RootDescriptors>,
    ) -> Result<Self, InitializationFailure> {
        let allow_root = roots.is_some();
        let (mut prepared, mut control) =
            PreparedTarget::prepare_initializing(config, spec, executable, roots)?;
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
            Ok::<_, io::Error>((init_pid, ids, pidfd))
        })();
        match initialize {
            Ok((init_pid, ids, init_pidfd)) => Ok(Self {
                prepared,
                init_pid,
                ids,
                control,
                init_pidfd,
                allow_root,
                root_ready: false,
                proc_dev_ready: false,
                inputs_ready: false,
                rlimits_ready: false,
                identity_ready: false,
                seccomp_ready: false,
            }),
            Err(error) => Err(prepared.refuse_at(ExecutionStage::Namespaces, Some(&error))),
        }
    }

    pub(crate) fn initialize_root(
        &mut self,
        budget: crate::isolation::root::RootBudget,
    ) -> std::io::Result<()> {
        if !self.allow_root || self.root_ready {
            return Err(denied("root descriptors not inherited"));
        }
        self.initialize_filesystem(
            NamespaceCommand::PrepareRoot {
                work_bytes: budget.work_bytes,
                tmp_bytes: budget.tmp_bytes,
            },
            NamespaceEvent::RootInitialized {},
        )?;
        self.root_ready = true;
        Ok(())
    }

    pub(crate) fn initialize_proc_dev(&mut self) -> io::Result<()> {
        if !self.root_ready || self.proc_dev_ready {
            return Err(denied("proc/dev initialization order"));
        }
        self.initialize_filesystem(
            NamespaceCommand::PrepareProcDev {},
            NamespaceEvent::ProcDevInitialized {},
        )?;
        self.proc_dev_ready = true;
        Ok(())
    }

    pub(crate) fn initialize_inputs(
        &mut self,
        inputs: &[crate::artifacts::PreparedInput],
    ) -> io::Result<()> {
        if !self.proc_dev_ready || self.inputs_ready || inputs.len() > 64 {
            return Err(denied("input initialization order"));
        }
        let bytes = inputs
            .iter()
            .try_fold(0_u64, |total, input| {
                total
                    .checked_add(input.file.bytes())
                    .filter(|n| *n <= crate::artifacts::MAX_SNAPSHOT_BYTES)
            })
            .ok_or_else(|| denied("input bytes"))?;
        self.control.send(&NamespaceCommand::PrepareInputs {
            count: inputs.len() as u32,
            bytes,
        })?;
        self.prepared
            .cgroup
            .begin_root_initialization(&[self.prepared.pid(), self.init_pid], &self.init_pidfd)?;
        self.expect_event(NamespaceEvent::InputsStarted {})?;
        for input in inputs {
            self.control.send(&NamespaceCommand::AttachInput {
                path: input.path.clone(),
                bytes: input.file.bytes(),
                executable: input.executable,
            })?;
            self.control.send_fd(&input.file)?;
            self.expect_event(NamespaceEvent::InputAttached {})?;
        }
        self.control.send(&NamespaceCommand::FinishInputs {})?;
        self.expect_event(NamespaceEvent::InputsInitialized {})?;
        self.stop_fixed_stage()?;
        self.inputs_ready = true;
        Ok(())
    }

    pub(crate) fn initialize_rlimits(
        &mut self,
        plan: crate::isolation::rlimits::RlimitPlan,
    ) -> io::Result<()> {
        if !self.inputs_ready || self.rlimits_ready || !plan.valid() {
            return Err(denied("rlimit initialization order"));
        }
        self.initialize_filesystem(
            NamespaceCommand::PrepareRlimits { plan },
            NamespaceEvent::RlimitsInitialized {},
        )?;
        // Held, unreaped PID plus frozen exact-members cgroup prevents PID reuse
        // and user mutation during the independent supervisor kernel readback.
        use std::io::Read;
        let mut bytes = String::new();
        std::fs::File::open(format!("/proc/{}/limits", self.init_pid))?
            .take(4097)
            .read_to_string(&mut bytes)?;
        plan.verify_proc(&bytes)?;
        self.rlimits_ready = true;
        Ok(())
    }

    pub(crate) fn initialize_identity(&mut self, uid: u32, gid: u32) -> io::Result<()> {
        if !self.rlimits_ready
            || self.identity_ready
            || !crate::isolation::identity::valid(uid, gid)
        {
            return Err(denied("credential initialization order"));
        }
        self.initialize_filesystem(
            NamespaceCommand::PrepareIdentity { uid, gid },
            NamespaceEvent::IdentityInitialized {},
        )?;
        crate::isolation::identity::verify_status(
            &std::fs::read_to_string(format!("/proc/{}/status", self.init_pid))?,
            uid,
            gid,
        )?;
        self.identity_ready = true;
        Ok(())
    }

    pub(crate) fn initialize_seccomp(
        &mut self,
        profile: crate::spec::ExecutionProfile,
    ) -> io::Result<()> {
        if !self.identity_ready || self.seccomp_ready {
            return Err(denied("seccomp initialization order"));
        }
        let status = std::fs::read_to_string(format!("/proc/{}/status", self.init_pid))?;
        let before = status_value(&status, "Seccomp_filters:")?;
        self.initialize_filesystem(
            NamespaceCommand::PrepareSeccomp { profile },
            NamespaceEvent::SeccompInitialized {},
        )?;
        let after = crate::isolation::seccomp::filter_count(&std::fs::read_to_string(format!(
            "/proc/{}/status",
            self.init_pid
        ))?)?;
        if before.checked_add(1) != Some(after) {
            return Err(denied("seccomp filter count mismatch"));
        }
        self.seccomp_ready = true;
        Ok(())
    }

    fn expect_event(&mut self, expected: NamespaceEvent) -> io::Result<()> {
        match self.control.receive::<NamespaceEvent>()? {
            event if event == expected => Ok(()),
            NamespaceEvent::Failed { errno: Some(errno) } => {
                Err(io::Error::from_raw_os_error(i32::from(errno)))
            }
            _ => Err(denied("fixed initialization acknowledgement")),
        }
    }

    fn initialize_filesystem(
        &mut self,
        command: NamespaceCommand,
        expected: NamespaceEvent,
    ) -> io::Result<()> {
        self.control.send(&command)?;
        self.prepared
            .cgroup
            .begin_root_initialization(&[self.prepared.pid(), self.init_pid], &self.init_pidfd)?;
        self.expect_event(expected)?;
        self.stop_fixed_stage()
    }

    fn stop_fixed_stage(&mut self) -> io::Result<()> {
        rustix::process::pidfd_send_signal(&self.init_pidfd, Signal::STOP)?;
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let status = std::fs::read_to_string(format!("/proc/{}/status", self.init_pid))?;
            if status_value(&status, "PPid:")? != self.prepared.pid() {
                return Err(denied("root init parent mismatch"));
            }
            if status.lines().any(|l| l == "State:\tT (stopped)") {
                break;
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "root init stop deadline",
                ));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        self.prepared
            .cgroup
            .finish_namespace_initialization(&[self.prepared.pid(), self.init_pid])?;
        Ok(())
    }

    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn prepare_execution(
        &mut self,
        spec: &ValidatedSpec,
        stdio: crate::io::ChildPipes,
    ) -> io::Result<()> {
        if !self.seccomp_ready {
            return Err(denied("execution before verified isolation"));
        }
        stdio.validate_for_exec()?;
        self.control
            .execution_deadline(spec.spec().limits.wall_time_us)?;
        self.control.send(&NamespaceCommand::PrepareExecution {
            request: Box::new(spec.spec().clone()),
        })?;
        self.prepared
            .cgroup
            .begin_root_initialization(&[self.prepared.pid(), self.init_pid], &self.init_pidfd)?;
        for fd in [&stdio.input, &stdio.output, &stdio.error] {
            self.control.send_fd(fd)?;
        }
        self.expect_event(NamespaceEvent::ExecutionPrepared {})?;
        self.stop_fixed_stage()
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
