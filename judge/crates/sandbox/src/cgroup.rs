//! Descriptor-relative management of one operator-delegated cgroup v2 subtree.
//! No request controls host paths; this module never changes the system root.

use crate::launcher::blocked::BlockedTarget;
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::{SpecPolicy, ValidatedSpec};
use rustix::fd::OwnedFd;
use rustix::fs::{AtFlags, Mode, OFlags};
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Component, Path};
use std::time::{Duration, Instant};

const CGROUP2_MAGIC: u64 = 0x6367_7270;
const MAX_INTERFACE_BYTES: u64 = 4096;
const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

#[derive(Debug)]
pub struct CgroupError {
    pub source: io::Error,
    pub cleanup_error: Option<io::Error>,
}

impl From<io::Error> for CgroupError {
    fn from(source: io::Error) -> Self {
        Self {
            source,
            cleanup_error: None,
        }
    }
}

impl std::fmt::Display for CgroupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "cgroup operation failed: {:?}", self.source.kind())
    }
}
impl std::error::Error for CgroupError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AppliedLimits {
    pub memory_bytes: u64,
    pub swap_bytes: u64,
    pub processes: u32,
    pub cpu_quota_us: u64,
    pub cpu_period_us: u64,
    pub oom_group: bool,
}

pub struct DelegatedCgroup {
    fd: OwnedFd,
    policy: SpecPolicy,
    quota: u64,
    period: u64,
}

impl DelegatedCgroup {
    /// Config comes from the operator. Structural validation is not proof of
    /// config-file ownership, service delegation or an exec permit.
    /// The root must be empty, owned by this helper and not group/other writable.
    pub fn open(config: &ValidatedRuntimeConfig) -> Result<Self, CgroupError> {
        let config_data = config.config();
        let fd = open_directory(Path::new(&config_data.delegated_cgroup_root))?;
        let stat = rustix::fs::fstat(&fd).map_err(io::Error::from)?;
        if stat.st_uid != rustix::process::geteuid().as_raw() || stat.st_mode & 0o022 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unsafe delegated owner/mode",
            )
            .into());
        }
        if rustix::fs::fstatfs(&fd).map_err(io::Error::from)?.f_type as u64 != CGROUP2_MAGIC {
            return Err(invalid("not a cgroup v2 directory").into());
        }
        if read(&fd, "cgroup.type")?.trim() != "domain"
            || !read(&fd, "cgroup.procs")?.trim().is_empty()
        {
            return Err(invalid("delegated root must be an empty domain").into());
        }
        let controllers = read(&fd, "cgroup.controllers")?;
        if !["cpu", "memory", "pids"]
            .iter()
            .all(|name| controllers.split_whitespace().any(|value| value == *name))
        {
            return Err(invalid("required controllers unavailable").into());
        }
        write(&fd, "cgroup.subtree_control", "+cpu +memory +pids")?;
        let enabled = read(&fd, "cgroup.subtree_control")?;
        if !["cpu", "memory", "pids"]
            .iter()
            .all(|name| enabled.split_whitespace().any(|value| value == *name))
        {
            return Err(invalid("required controllers not enabled").into());
        }
        // Bound concurrent task directories and prevent deeper cgroup creation.
        write_verify(&fd, "cgroup.max.depth", "1")?;
        write_verify(
            &fd,
            "cgroup.max.descendants",
            &config_data.max_concurrent_runs.to_string(),
        )?;
        let budget = &config_data.aggregate;
        let aggregate = AppliedLimits {
            memory_bytes: budget.memory_bytes,
            swap_bytes: 0,
            processes: budget.processes,
            cpu_quota_us: budget.cpu_quota_us,
            cpu_period_us: budget.cpu_period_us,
            oom_group: true,
        };
        apply_limits(&fd, &aggregate)?;
        Ok(Self {
            fd,
            policy: config.spec_policy(),
            quota: budget.task_cpu_quota_us,
            period: budget.cpu_period_us,
        })
    }

    /// Always mkdir exclusive: never adopt, overwrite or reuse an existing task.
    pub fn create_task(&self, spec: &ValidatedSpec) -> Result<TaskCgroup, CgroupError> {
        spec.spec()
            .clone()
            .validate(&self.policy)
            .map_err(|_| invalid("request exceeds delegated policy"))?;
        let name = format!("kf-{}", spec.spec().run_id);
        rustix::fs::mkdirat(&self.fd, name.as_str(), Mode::from_raw_mode(0o700))
            .map_err(io::Error::from)?;
        let opened = rustix::fs::openat(&self.fd, name.as_str(), DIR_FLAGS, Mode::empty());
        let fd = match opened {
            Ok(fd) => fd,
            Err(error) => {
                return Err(CgroupError {
                    source: error.into(),
                    cleanup_error: rustix::fs::unlinkat(
                        &self.fd,
                        name.as_str(),
                        AtFlags::REMOVEDIR,
                    )
                    .err()
                    .map(Into::into),
                });
            }
        };
        let parent = match rustix::io::fcntl_dupfd_cloexec(&self.fd, 3) {
            Ok(fd) => fd,
            Err(error) => {
                return Err(CgroupError {
                    source: error.into(),
                    cleanup_error: rustix::fs::unlinkat(
                        &self.fd,
                        name.as_str(),
                        AtFlags::REMOVEDIR,
                    )
                    .err()
                    .map(Into::into),
                });
            }
        };
        let limits = AppliedLimits {
            memory_bytes: spec.spec().limits.memory_bytes,
            swap_bytes: 0,
            processes: spec.spec().limits.processes,
            cpu_quota_us: self.quota,
            cpu_period_us: self.period,
            oom_group: true,
        };
        let mut task = TaskCgroup {
            parent,
            fd,
            name,
            limits,
            removed: false,
        };
        let setup = (|| {
            if read(&task.fd, "cgroup.type")?.trim() != "domain" {
                return Err(invalid("invalid task domain"));
            }
            apply_limits(&task.fd, &task.limits)?;
            write_verify(&task.fd, "cgroup.max.depth", "0")?;
            write_verify(&task.fd, "cgroup.max.descendants", "0")?;
            // Make fatal termination cover the entire task and retain a kernel
            // freeze gate even if somebody sends the bootstrap SIGCONT.
            write_verify(&task.fd, "cgroup.freeze", "1")?;
            task.wait_event("frozen", "1", Duration::from_secs(1))?;
            Ok::<_, io::Error>(())
        })();
        if let Err(source) = setup {
            let cleanup_error = task.kill_and_remove(Duration::from_secs(1)).err();
            return Err(CgroupError {
                source,
                cleanup_error,
            });
        }
        Ok(task)
    }
}

pub struct TaskCgroup {
    parent: OwnedFd,
    fd: OwnedFd,
    name: String,
    limits: AppliedLimits,
    removed: bool,
}

impl TaskCgroup {
    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn applied_limits(&self) -> io::Result<AppliedLimits> {
        self.live()?;
        verify_limits(&self.fd, &self.limits)?;
        Ok(self.limits.clone())
    }

    /// Private fixed initialization only, never a public user-exec release.
    pub(crate) fn begin_namespace_initialization(
        &mut self,
        target: &mut BlockedTarget,
    ) -> io::Result<()> {
        target.verify_stopped()?;
        self.applied_limits()?;
        self.wait_event("frozen", "1", Duration::from_secs(1))?;
        self.verify_members(&[target.pid()])?;
        write_verify(&self.fd, "cgroup.freeze", "0")?;
        self.wait_event("frozen", "0", Duration::from_secs(1))?;
        target.continue_initialization()
    }

    pub(crate) fn finish_namespace_initialization(&mut self, members: &[u32]) -> io::Result<()> {
        self.verify_members(members)?;
        self.applied_limits()?;
        write_verify(&self.fd, "cgroup.freeze", "1")?;
        self.wait_event("frozen", "1", Duration::from_secs(1))?;
        self.verify_members(members)
    }

    pub(crate) fn begin_root_initialization(
        &mut self,
        members: &[u32],
        init_pidfd: &OwnedFd,
    ) -> io::Result<()> {
        self.verify_members(members)?;
        self.applied_limits()?;
        self.wait_event("frozen", "1", Duration::from_secs(1))?;
        write_verify(&self.fd, "cgroup.freeze", "0")?;
        self.wait_event("frozen", "0", Duration::from_secs(1))?;
        Ok(rustix::process::pidfd_send_signal(
            init_pidfd,
            rustix::process::Signal::CONT,
        )?)
    }

    fn verify_members(&self, expected: &[u32]) -> io::Result<()> {
        let mut members = read(&self.fd, "cgroup.procs")?
            .split_whitespace()
            .map(|value| {
                value
                    .parse::<u32>()
                    .map_err(|_| invalid("invalid cgroup member"))
            })
            .collect::<io::Result<Vec<_>>>()?;
        let mut expected = expected.to_vec();
        members.sort_unstable();
        expected.sort_unstable();
        if members != expected {
            return Err(invalid("unexpected cgroup members"));
        }
        Ok(())
    }

    /// Read back the kernel freeze gate before inspecting task-owned files.
    pub(crate) fn verify_frozen_members(&self, expected: &[u32]) -> io::Result<()> {
        self.live()?;
        self.applied_limits()?;
        self.wait_event("frozen", "1", Duration::from_secs(1))?;
        self.verify_members(expected)
    }

    /// PID comes from the unreaped direct Child; callers cannot enroll arbitrary PIDs.
    pub fn enroll(&mut self, target: &mut BlockedTarget) -> Result<(), CgroupError> {
        self.live()?;
        target.verify_stopped()?;
        self.applied_limits()?;
        self.wait_event("frozen", "1", Duration::from_secs(1))?;
        if !read(&self.fd, "cgroup.procs")?.trim().is_empty() {
            return Err(invalid("task already populated").into());
        }
        write(&self.fd, "cgroup.procs", &target.pid().to_string())?;
        let members = read(&self.fd, "cgroup.procs")?;
        if members.trim() != target.pid().to_string() {
            return Err(invalid("task enrollment mismatch").into());
        }
        target.verify_stopped()?;
        self.wait_event("frozen", "1", Duration::from_secs(1))?;
        Ok(())
    }

    /// Entire group must be empty before descriptor-relative removal. Repeated
    /// cleanup is harmless; explicit calls report errors independently.
    pub fn kill_and_remove(&mut self, timeout: Duration) -> io::Result<()> {
        if self.removed {
            return Ok(());
        }
        if timeout.is_zero() || timeout > Duration::from_secs(5) {
            return Err(invalid("invalid cleanup deadline"));
        }
        write(&self.fd, "cgroup.kill", "1")?;
        self.wait_event("populated", "0", timeout)?;
        rustix::fs::unlinkat(&self.parent, self.name.as_str(), AtFlags::REMOVEDIR)?;
        self.removed = true;
        Ok(())
    }

    fn live(&self) -> io::Result<()> {
        if self.removed {
            Err(io::Error::new(
                io::ErrorKind::NotFound,
                "task cgroup removed",
            ))
        } else {
            Ok(())
        }
    }

    fn wait_event(&self, key: &str, expected: &str, timeout: Duration) -> io::Result<()> {
        let deadline = Instant::now() + timeout;
        loop {
            let events = read(&self.fd, "cgroup.events")?;
            if events
                .lines()
                .any(|line| line.split_once(' ') == Some((key, expected)))
            {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "cgroup event deadline",
                ));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
}

impl Drop for TaskCgroup {
    fn drop(&mut self) {
        if !self.removed {
            let _ = self.kill_and_remove(Duration::from_millis(250));
        }
    }
}

fn open_directory(path: &Path) -> io::Result<OwnedFd> {
    // The validated root is absolute and strictly below /sys/fs/cgroup. Walk
    // every component with NOFOLLOW; a symlink ancestor cannot redirect us.
    if !path.starts_with("/sys/fs/cgroup") || path == Path::new("/sys/fs/cgroup") {
        return Err(invalid("not a delegated cgroup subtree"));
    }
    let mut fd = rustix::fs::open("/", DIR_FLAGS, Mode::empty())?;
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => {
                fd = rustix::fs::openat(&fd, name, DIR_FLAGS, Mode::empty())?
            }
            _ => return Err(invalid("noncanonical cgroup root")),
        }
    }
    Ok(fd)
}

fn read(fd: &OwnedFd, name: &str) -> io::Result<String> {
    let file = rustix::fs::openat(
        fd,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let mut text = String::new();
    File::from(file)
        .take(MAX_INTERFACE_BYTES + 1)
        .read_to_string(&mut text)?;
    if text.len() as u64 > MAX_INTERFACE_BYTES {
        return Err(invalid("cgroup interface too large"));
    }
    Ok(text)
}

fn write(fd: &OwnedFd, name: &str, value: &str) -> io::Result<()> {
    let file = rustix::fs::openat(
        fd,
        name,
        OFlags::WRONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    // Kernfs values (not streams): one complete write, never retry a short suffix.
    if File::from(file).write(value.as_bytes())? != value.len() {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "short cgroup write",
        ));
    }
    Ok(())
}

fn write_verify(fd: &OwnedFd, name: &str, value: &str) -> io::Result<()> {
    write(fd, name, value)?;
    if read(fd, name)?.trim() != value {
        return Err(invalid("cgroup readback mismatch"));
    }
    Ok(())
}

fn apply_limits(fd: &OwnedFd, limits: &AppliedLimits) -> io::Result<()> {
    write_verify(fd, "memory.max", &limits.memory_bytes.to_string())?;
    write_verify(fd, "memory.swap.max", &limits.swap_bytes.to_string())?;
    write_verify(
        fd,
        "memory.oom.group",
        if limits.oom_group { "1" } else { "0" },
    )?;
    write_verify(fd, "pids.max", &limits.processes.to_string())?;
    write_verify(
        fd,
        "cpu.max",
        &format!("{} {}", limits.cpu_quota_us, limits.cpu_period_us),
    )
}

fn verify_limits(fd: &OwnedFd, limits: &AppliedLimits) -> io::Result<()> {
    for (name, expected) in [
        ("memory.max", limits.memory_bytes.to_string()),
        ("memory.swap.max", limits.swap_bytes.to_string()),
        ("pids.max", limits.processes.to_string()),
        (
            "memory.oom.group",
            if limits.oom_group { "1" } else { "0" }.into(),
        ),
        (
            "cpu.max",
            format!("{} {}", limits.cpu_quota_us, limits.cpu_period_us),
        ),
    ] {
        if read(fd, name)?.trim() != expected {
            return Err(invalid("configured limits changed"));
        }
    }
    Ok(())
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn system_root_and_host_paths_are_rejected_before_open() {
        for path in [
            "/sys/fs/cgroup",
            "/tmp/cgroup",
            "/sys/fs/cgroup/../cgroup",
            "/sys/fs/cgroup/./task",
        ] {
            assert!(open_directory(Path::new(path)).is_err());
        }
    }
}
