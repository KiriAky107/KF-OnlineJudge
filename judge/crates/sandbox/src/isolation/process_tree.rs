//! Single-threaded namespace PID 1. Private until all execution gates are ready.
//! The init never execs the user program: it owns a pidfd and reaps orphaned
//! descendants. Exiting init is the namespace-wide cancellation mechanism.
use crate::{outcome::Termination, spec::ValidatedSpec};
use rustix::{
    fd::{AsFd, AsRawFd, OwnedFd},
    process::{Pid, PidfdFlags, WaitId, WaitIdOptions, WaitOptions, WaitStatus},
};
use std::{ffi::CString, io};

fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

pub(crate) use crate::io::ChildPipes as ChildStdio;

/// Allocate/validate strings before fork. No request shell or host environment.
pub(crate) struct ExecPlan {
    argv: Vec<CString>,
    env: Vec<CString>,
    cwd: CString,
}
impl ExecPlan {
    pub(crate) fn new(spec: &ValidatedSpec) -> io::Result<Self> {
        let string = |s: String| CString::new(s).map_err(|_| denied("exec string"));
        Ok(Self {
            argv: spec
                .spec()
                .argv
                .iter()
                .cloned()
                .map(string)
                .collect::<io::Result<_>>()?,
            env: spec
                .execution_environment()
                .into_iter()
                .map(|(k, v)| string(format!("{k}={v}")))
                .collect::<io::Result<_>>()?,
            cwd: string(if spec.spec().cwd == "." {
                "/work".into()
            } else {
                format!("/work/{}", spec.spec().cwd)
            })?,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ExecObservation {
    Pending,
    NoErrorObserved,
    Failed { errno: u16 },
}
#[derive(Clone, Debug)]
pub(crate) struct TreeObservation {
    pub main: Option<Termination>,
    pub exec: ExecObservation,
    pub reaped_descendants: u32,
    pub empty: bool,
}
pub(crate) enum NamespaceEnd {
    Drained,
    Cancelled,
    ControlLost,
    MonitorFailed,
}

pub(crate) struct Reaper {
    pid: Pid,
    pidfd: OwnedFd,
    exec_error: OwnedFd,
    errno_bytes: [u8; 4],
    errno_len: usize,
    observation: TreeObservation,
}
impl Reaper {
    pub(crate) fn spawn(plan: ExecPlan, stdio: ChildStdio) -> io::Result<Self> {
        crate::launcher::entry::ensure_single_threaded()?;
        if rustix::process::getpid() != Pid::INIT {
            return Err(denied("reaper requires namespace PID 1"));
        }
        let uid = rustix::process::getuid().as_raw();
        let gid = rustix::process::getgid().as_raw();
        let status = std::fs::read_to_string("/proc/self/status")?;
        if !crate::isolation::identity::valid(uid, gid) {
            return Err(denied("reaper identity"));
        }
        crate::isolation::identity::verify_status(&status, uid, gid)?;
        crate::isolation::seccomp::filter_count(&status)?;
        if rustix::process::dumpable_behavior()? != rustix::process::DumpableBehavior::NotDumpable {
            return Err(denied("trusted init descriptor privacy"));
        }
        // PID ownership requires no inherited SIGCHLD auto-reap disposition.
        check_sigchld()?;
        stdio.validate_for_exec()?;
        let (read, write) = rustix::pipe::pipe_with(rustix::pipe::PipeFlags::CLOEXEC)?;
        rustix::fs::fcntl_setfl(&read, rustix::fs::OFlags::NONBLOCK)?;
        let pid = fork_exec(&plan, &stdio, &write)?;
        drop(write);
        drop(stdio); // Init must not keep users' pipe ends open.
        let pidfd = match rustix::process::pidfd_open(pid, PidfdFlags::empty()) {
            Ok(fd) => fd,
            Err(_) => std::process::exit(125), // PID 1 exit kills the spawned tree.
        };
        Ok(Self {
            pid,
            pidfd,
            exec_error: read,
            errno_bytes: [0; 4],
            errno_len: 0,
            observation: TreeObservation {
                main: None,
                exec: ExecObservation::Pending,
                reaped_descendants: 0,
                empty: false,
            },
        })
    }
    pub(crate) fn poll(&mut self) -> io::Result<TreeObservation> {
        self.poll_exec()?;
        if self.observation.main.is_none() {
            match rustix::process::waitid(
                WaitId::PidFd(self.pidfd.as_fd()),
                WaitIdOptions::EXITED | WaitIdOptions::NOHANG,
            ) {
                Ok(Some(s)) => {
                    self.observation.main = Some(if let Some(code) = s.exit_status() {
                        Termination::Exited { code: code as u8 }
                    } else if let Some(signal) = s.terminating_signal() {
                        Termination::Signaled {
                            signal: signal as u8,
                            core_dumped: s.dumped(),
                        }
                    } else {
                        return Err(denied("unexpected main waitid state"));
                    });
                }
                Ok(None) | Err(rustix::io::Errno::INTR) => {}
                Err(e) => return Err(e.into()),
            }
        }
        // At most the validated pids ceiling per tick; don't starve control/I/O.
        for _ in 0..128 {
            match rustix::process::wait(WaitOptions::NOHANG) {
                Ok(Some((pid, status))) if pid == self.pid => {
                    // Main exited between pidfd polling and wait-any. Record
                    // its actual status, never mistake it for a descendant.
                    if self.observation.main.is_some() {
                        return Err(denied("main reaped twice"));
                    }
                    self.observation.main = Some(termination(status)?);
                }
                Ok(Some(_)) => {
                    self.observation.reaped_descendants = self
                        .observation
                        .reaped_descendants
                        .checked_add(1)
                        .ok_or_else(|| denied("reap counter overflow"))?
                }
                Ok(None) => break,
                Err(rustix::io::Errno::INTR) => continue,
                Err(rustix::io::Errno::CHILD) => {
                    if self.observation.main.is_none() {
                        return Err(denied("main wait ownership lost"));
                    }
                    self.observation.empty = true;
                    break;
                }
                Err(e) => return Err(e.into()),
            }
        }
        if self.observation.main.is_some() {
            self.poll_exec()?;
        }
        Ok(self.observation.clone())
    }
    fn poll_exec(&mut self) -> io::Result<()> {
        if self.observation.exec != ExecObservation::Pending {
            return Ok(());
        }
        loop {
            match rustix::io::read(&self.exec_error, &mut self.errno_bytes[self.errno_len..]) {
                Ok(0) if self.errno_len == 0 => {
                    self.observation.exec = ExecObservation::NoErrorObserved;
                    return Ok(());
                }
                Ok(0) => return Err(denied("partial exec error")),
                Ok(n) => {
                    self.errno_len += n;
                    if self.errno_len == 4 {
                        let errno = i32::from_ne_bytes(self.errno_bytes);
                        let errno = u16::try_from(errno)
                            .ok()
                            .filter(|n| *n > 0)
                            .ok_or_else(|| denied("exec errno"))?;
                        self.observation.exec = ExecObservation::Failed { errno };
                        return Ok(());
                    }
                }
                Err(rustix::io::Errno::AGAIN) => return Ok(()),
                Err(rustix::io::Errno::INTR) => continue,
                Err(e) => return Err(e.into()),
            }
        }
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn main_pidfd(&self) -> &OwnedFd {
        &self.pidfd
    }
    pub(crate) fn shutdown(self, reason: NamespaceEnd) -> ! {
        let code = match reason {
            NamespaceEnd::Drained | NamespaceEnd::Cancelled => 0,
            NamespaceEnd::ControlLost | NamespaceEnd::MonitorFailed => 125,
        };
        // Constructor proved PID 1. Kernel SIGKILLs and waits for the entire
        // namespace; the outside supervisor must still verify cgroup empty.
        std::process::exit(code)
    }
}
impl Drop for Reaper {
    fn drop(&mut self) {
        // Monitor panic/error cannot abandon a live descendant namespace.
        std::process::exit(125);
    }
}
fn termination(s: WaitStatus) -> io::Result<Termination> {
    if let Some(code) = s.exit_status() {
        Ok(Termination::Exited { code: code as u8 })
    } else if let Some(signal) = s.terminating_signal() {
        Ok(Termination::Signaled {
            signal: signal as u8,
            core_dumped: s.as_raw() & 0x80 != 0,
        })
    } else {
        Err(denied("unexpected descendant wait state"))
    }
}

#[allow(unsafe_code)]
fn check_sigchld() -> io::Result<()> {
    // SAFETY: sigaction query initializes the supplied C struct, no handler or
    // global disposition is changed. Only the checked single-threaded init.
    unsafe {
        let mut s: libc::sigaction = std::mem::zeroed();
        if libc::sigaction(libc::SIGCHLD, std::ptr::null(), &mut s) != 0 {
            return Err(io::Error::last_os_error());
        }
        if s.sa_sigaction != libc::SIG_DFL || s.sa_flags & libc::SA_NOCLDWAIT != 0 {
            return Err(denied("SIGCHLD must preserve wait ownership"));
        }
    }
    Ok(())
}

#[allow(unsafe_code)]
fn fork_exec(plan: &ExecPlan, stdio: &ChildStdio, error: &OwnedFd) -> io::Result<Pid> {
    let mut argv: Vec<_> = plan.argv.iter().map(|s| s.as_ptr()).collect();
    argv.push(std::ptr::null());
    let mut env: Vec<_> = plan.env.iter().map(|s| s.as_ptr()).collect();
    env.push(std::ptr::null());
    let sources = [
        stdio.input.as_raw_fd(),
        stdio.output.as_raw_fd(),
        stdio.error.as_raw_fd(),
    ];
    let err = error.as_raw_fd();
    let program = plan.argv[0].as_ptr();
    let cwd = plan.cwd.as_ptr();
    if err < 4 {
        return Err(denied("exec-error descriptor alias"));
    }
    // SAFETY: fresh, checked single-threaded PID 1, trusted image only, no user
    // code/threads/atfork hooks loaded. All allocations/C strings/raw FD values
    // prepared above and borrowed alive in both processes. Child uses only
    // async-signal-safe libc syscalls and exec/_exit, never Rust allocation,
    // panic, unwinding or descriptor destructors. fork copies, not shares, the
    // FD table; only its copy is rewritten/closed. All source FDs are >=4.
    unsafe {
        let raw = libc::fork();
        if raw < 0 {
            return Err(io::Error::last_os_error());
        }
        if raw == 0 {
            if libc::dup3(err, 3, libc::O_CLOEXEC) < 0 {
                let errno = *libc::__errno_location();
                let _ = libc::write(err, (&errno as *const i32).cast(), 4);
                libc::_exit(126);
            }
            let setup = sources
                .iter()
                .enumerate()
                .all(|(target, source)| libc::dup2(*source, target as i32) >= 0)
                && libc::syscall(libc::SYS_close_range, 4_u32, u32::MAX, 0_u32) == 0
                && libc::chdir(cwd) == 0
                && libc::signal(libc::SIGPIPE, libc::SIG_DFL) != libc::SIG_ERR;
            if setup {
                libc::execve(program, argv.as_ptr(), env.as_ptr());
            }
            let errno = *libc::__errno_location();
            let _ = libc::write(3, (&errno as *const i32).cast(), 4);
            libc::_exit(126);
        }
        Ok(Pid::from_raw(raw).expect("positive fork PID"))
    }
}
