//! Namespace policy and kernel operations, restricted to a fresh trusted helper.

use std::io;
use std::os::unix::fs::MetadataExt;

/// Kernel namespace inode identities, observed by the trusted supervisor.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NamespaceIds {
    pub mount: u64,
    pub pid: u64,
    pub ipc: u64,
    pub uts: u64,
    pub network: u64,
    pub cgroup: u64,
    pub user: u64,
}

impl NamespaceIds {
    pub fn observe(pid: u32) -> io::Result<Self> {
        let inode = |name| std::fs::metadata(format!("/proc/{pid}/ns/{name}")).map(|m| m.ino());
        Ok(Self {
            mount: inode("mnt")?,
            pid: inode("pid")?,
            ipc: inode("ipc")?,
            uts: inode("uts")?,
            network: inode("net")?,
            cgroup: inode("cgroup")?,
            user: inode("user")?,
        })
    }

    pub(crate) fn verify_separate(&self, parent: &Self) -> io::Result<()> {
        if self.mount == parent.mount
            || self.pid == parent.pid
            || self.ipc == parent.ipc
            || self.uts == parent.uts
            || self.network == parent.network
            || self.cgroup == parent.cgroup
            || self.user != parent.user
        {
            return Err(io::Error::other(
                "namespace/privileged-helper identity mismatch",
            ));
        }
        Ok(())
    }
}

/// One narrowly scoped unsafe call: rustix requires unsafe for all unshare
/// flags because FILES can split FD tables. We never use FILES or NEWUSER,
/// and run only in a fresh, verified single-threaded trusted helper.
#[allow(unsafe_code)]
pub(crate) fn create() -> io::Result<()> {
    crate::launcher::entry::ensure_single_threaded()?;
    // P0 policy: privileged initialization in the original user namespace,
    // followed (in a later stage) by distinct task UID/GID and capability
    // removal. Single-UID NEWUSER mapping is deliberately unsupported.
    if rustix::process::geteuid().as_raw() != 0
        || rustix::process::getuid().as_raw() != 0
        || std::fs::metadata("/proc/self/ns/user")?.ino()
            != std::fs::metadata("/proc/1/ns/user")?.ino()
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "requires original-user-namespace privileged helper",
        ));
    }
    let flags = rustix::thread::UnshareFlags::NEWNS
        | rustix::thread::UnshareFlags::NEWPID
        | rustix::thread::UnshareFlags::NEWIPC
        | rustix::thread::UnshareFlags::NEWUTS
        | rustix::thread::UnshareFlags::NEWNET
        | rustix::thread::UnshareFlags::NEWCGROUP;
    // SAFETY: verified single thread, no FILES flag, no foreign FD-table users.
    unsafe {
        rustix::thread::unshare_unsafe(flags)?;
    }
    rustix::mount::mount_change(
        "/",
        rustix::mount::MountPropagationFlags::PRIVATE | rustix::mount::MountPropagationFlags::REC,
    )?;
    rustix::system::sethostname(b"kf-sandbox")?;
    Ok(())
}
