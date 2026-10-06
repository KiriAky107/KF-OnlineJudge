//! Real kernel stop before task enrollment. There is deliberately no exec/release API.

use crate::cgroup::{DelegatedCgroup, TaskCgroup};
use crate::outcome::{ErrorCode, ExecutionError, ExecutionStage};
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use rustix::fd::{AsFd, OwnedFd};
use rustix::process::{Pid, PidfdFlags, Signal, WaitId, WaitIdOptions};
use std::io;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

const STARTUP: Duration = Duration::from_secs(5);
const CLEANUP: Duration = Duration::from_secs(2);

/// Runs only after a fresh exec, without custom pre_exec/fork callbacks. A parent
/// dying before/during prctl is caught by the subsequent kernel parent check.
/// PDEATHSIG refers to the spawning thread: the supervisor must keep it alive.
pub fn bootstrap(expected_parent: u32) -> io::Result<()> {
    super::entry::ensure_single_threaded()?;
    rustix::process::set_parent_process_death_signal(Some(Signal::KILL))?;
    let parent = rustix::process::getppid();
    if expected_parent <= 1
        || parent.map(|pid| pid.as_raw_pid() as u32) != Some(expected_parent)
        || rustix::process::parent_process_death_signal()? != Some(Signal::KILL)
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "bootstrap parent mismatch",
        ));
    }
    rustix::process::kill_process(rustix::process::getpid(), Signal::STOP)?;
    // An unexpected SIGCONT cannot run initialization or a user command. Future
    // isolation initialization and the separate final exec gate are still absent.
    Err(io::Error::new(
        io::ErrorKind::PermissionDenied,
        "bootstrap has no execution permit",
    ))
}

/// Sole child owner; callers must not ignore SIGCHLD or reap this child elsewhere.
/// pidfd is obtained before any wait; NOWAIT leaves reaping to the Child handle.
pub struct BlockedTarget {
    child: Child,
    pidfd: OwnedFd,
    reaped: bool,
}

impl BlockedTarget {
    /// `executable` is the operator's trusted sandbox binary, never a request argv.
    pub fn spawn(executable: &Path) -> io::Result<Self> {
        // A ignored/caught SIGCHLD could reap/recycle a PID before pidfd_open.
        // Fresh helpers retain the default disposition; reject inherited changes.
        let status = std::fs::read_to_string("/proc/self/status")?;
        for key in ["SigIgn:", "SigCgt:"] {
            let bits = status
                .lines()
                .find_map(|line| line.strip_prefix(key))
                .and_then(|value| u64::from_str_radix(value.trim(), 16).ok())
                .ok_or_else(|| io::Error::other("cannot verify SIGCHLD policy"))?;
            if bits & (1 << (rustix::process::Signal::CHILD.as_raw() - 1)) != 0 {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "SIGCHLD must have default disposition",
                ));
            }
        }
        let mut child = Command::new(executable)
            .arg("bootstrap")
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let Some(pid) = Pid::from_raw(child.id() as i32) else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::other("invalid child PID"));
        };
        let pidfd = match rustix::process::pidfd_open(pid, PidfdFlags::empty()) {
            Ok(fd) => fd,
            Err(error) => {
                // The unreaped direct child cannot have its PID recycled.
                let _ = child.kill();
                let _ = child.wait();
                return Err(error.into());
            }
        };
        let mut target = Self {
            child,
            pidfd,
            reaped: false,
        };
        let deadline = Instant::now() + STARTUP;
        loop {
            let state = rustix::process::waitid(
                WaitId::PidFd(target.pidfd.as_fd()),
                WaitIdOptions::STOPPED
                    | WaitIdOptions::EXITED
                    | WaitIdOptions::NOHANG
                    | WaitIdOptions::NOWAIT,
            )?;
            if let Some(state) = state {
                if state.stopping_signal() == Some(Signal::STOP.as_raw()) {
                    target.verify_stopped()?;
                    return Ok(target);
                }
                return Err(io::Error::other("bootstrap exited before stop"));
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "bootstrap stop deadline",
                ));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn poll_exit(&mut self) -> io::Result<Option<ExitStatus>> {
        let status = self.child.try_wait()?;
        if status.is_some() {
            self.reaped = true;
        }
        Ok(status)
    }

    pub fn verify_stopped(&mut self) -> io::Result<()> {
        if self.reaped || self.child.try_wait()?.is_some() {
            self.reaped = true;
            return Err(io::Error::other("bootstrap is not alive"));
        }
        let status = std::fs::read_to_string(format!("/proc/{}/status", self.pid()))?;
        if !status.lines().any(|line| line == "State:\tT (stopped)") {
            return Err(io::Error::other("bootstrap is not kernel stopped"));
        }
        Ok(())
    }

    pub fn terminate(&mut self) -> io::Result<ExitStatus> {
        if self.reaped {
            return self.child.wait();
        }
        match rustix::process::pidfd_send_signal(&self.pidfd, Signal::KILL) {
            Ok(()) | Err(rustix::io::Errno::SRCH) => {}
            Err(error) => return Err(error.into()),
        }
        let deadline = Instant::now() + CLEANUP;
        loop {
            if let Some(status) = self.child.try_wait()? {
                self.reaped = true;
                return Ok(status);
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "bootstrap reap deadline",
                ));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
}

impl Drop for BlockedTarget {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.terminate();
        }
    }
}

#[derive(Debug)]
pub struct InitializationFailure {
    pub failure: ExecutionError,
    pub cleanup_failure: Option<ExecutionError>,
}

fn failure(stage: ExecutionStage, code: ErrorCode, error: Option<&io::Error>) -> ExecutionError {
    ExecutionError {
        stage,
        code,
        errno: error
            .and_then(|e| e.raw_os_error())
            .and_then(|n| u16::try_from(n).ok()),
    }
}

/// Owns the real stopped child and its frozen task cgroup. No lifecycle event,
/// JSON field or public method can turn this into a user-code execution permit.
pub struct PreparedTarget {
    target: BlockedTarget,
    cgroup: TaskCgroup,
}

impl PreparedTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
    ) -> Result<Self, InitializationFailure> {
        let mut target =
            BlockedTarget::spawn(executable).map_err(|error| InitializationFailure {
                failure: failure(
                    ExecutionStage::Preparation,
                    ErrorCode::OperationFailed,
                    Some(&error),
                ),
                cleanup_failure: None,
            })?;
        let setup = (|| {
            let root = DelegatedCgroup::open(config)?;
            let mut group = root.create_task(spec)?;
            if let Err(mut error) = group.enroll(&mut target) {
                error.cleanup_error = group.kill_and_remove(CLEANUP).err();
                return Err(error);
            }
            Ok::<_, crate::cgroup::CgroupError>(group)
        })();
        match setup {
            Ok(cgroup) => Ok(Self { target, cgroup }),
            Err(error) => {
                let reap_error = target.terminate().err();
                Err(InitializationFailure {
                    failure: failure(
                        ExecutionStage::Cgroup,
                        ErrorCode::OperationFailed,
                        Some(&error.source),
                    ),
                    cleanup_failure: error.cleanup_error.as_ref().or(reap_error.as_ref()).map(
                        |e| {
                            failure(
                                ExecutionStage::Cleanup,
                                ErrorCode::CleanupIncomplete,
                                Some(e),
                            )
                        },
                    ),
                })
            }
        }
    }

    pub fn pid(&self) -> u32 {
        self.target.pid()
    }
    pub fn cgroup(&self) -> &TaskCgroup {
        &self.cgroup
    }

    /// Missing isolation is a real initialization failure, never readiness.
    /// Kill/reap happens before a refusal is returned to the caller.
    pub fn refuse_unimplemented_isolation(mut self) -> InitializationFailure {
        let group_error = self.cgroup.kill_and_remove(CLEANUP).err();
        let reap_error = self.target.terminate().err();
        InitializationFailure {
            failure: failure(
                ExecutionStage::Namespaces,
                ErrorCode::CapabilityUnavailable,
                None,
            ),
            cleanup_failure: group_error.as_ref().or(reap_error.as_ref()).map(|e| {
                failure(
                    ExecutionStage::Cleanup,
                    ErrorCode::CleanupIncomplete,
                    Some(e),
                )
            }),
        }
    }
}

impl Drop for PreparedTarget {
    fn drop(&mut self) {
        let _ = self.cgroup.kill_and_remove(CLEANUP);
        let _ = self.target.terminate();
    }
}
