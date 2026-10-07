//! Exclusive per-attempt tmpfs in the fresh supervisor's private mount namespace.
use crate::{runtime::ValidatedRuntimeConfig, spec::ValidatedSpec};
use rustix::{
    fd::{AsRawFd, OwnedFd},
    fs::{AtFlags, Mode, OFlags},
    mount::{MountFlags, UnmountFlags},
};
use std::{io, path::Path};
const DIR: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
#[allow(unsafe_code)]
pub(crate) fn private_supervisor() -> io::Result<()> {
    super::entry::ensure_single_threaded()?;
    // SAFETY: fresh single-threaded supervisor, mount namespace only; no shared FD table change.
    unsafe {
        rustix::thread::unshare_unsafe(rustix::thread::UnshareFlags::NEWNS)?;
    }
    rustix::mount::mount_change(
        "/",
        rustix::mount::MountPropagationFlags::PRIVATE | rustix::mount::MountPropagationFlags::REC,
    )?;
    Ok(())
}
pub(crate) struct CaptureLease {
    parent: OwnedFd,
    name: String,
    path: String,
    mounted: bool,
    removed: bool,
}
impl CaptureLease {
    pub(crate) fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
    ) -> io::Result<(Self, OwnedFd)> {
        let total = spec
            .spec()
            .limits
            .stdout_bytes
            .checked_add(spec.spec().limits.stderr_bytes)
            .ok_or_else(|| io::Error::other("capture overflow"))?;
        let size = total
            .max(1)
            .checked_add(4095)
            .ok_or_else(|| io::Error::other("capture overflow"))?
            / 4096
            * 4096;
        if size > crate::artifacts::MAX_SNAPSHOT_BYTES
            || size
                .checked_mul(config.config().max_concurrent_runs.into())
                .is_none_or(|n| n > config.config().aggregate.supervisor_workdir_bytes)
        {
            return Err(io::Error::other("aggregate capture reservation"));
        }
        let state = crate::isolation::root::open_directory(Path::new(&config.config().state_root))?;
        match rustix::fs::mkdirat(&state, "captures", Mode::from_raw_mode(0o700)) {
            Ok(()) | Err(rustix::io::Errno::EXIST) => {}
            Err(e) => return Err(e.into()),
        }
        let parent = rustix::fs::openat(&state, "captures", DIR, Mode::empty())?;
        let stat = rustix::fs::fstat(&parent)?;
        if stat.st_uid != 0 || stat.st_mode & 0o7777 != 0o700 {
            return Err(io::Error::other("capture parent ownership"));
        }
        let name = format!("kf-{}", spec.spec().run_id);
        rustix::fs::mkdirat(&parent, name.as_str(), Mode::from_raw_mode(0o700))?;
        let path = format!("/proc/self/fd/{}/{}", parent.as_raw_fd(), name);
        let mut lease = Self {
            parent,
            name,
            path,
            mounted: false,
            removed: false,
        };
        let mount = (|| {
            let data = std::ffi::CString::new(format!("size={size},nr_inodes=16,mode=0700"))?;
            rustix::mount::mount(
                "tmpfs",
                lease.path.as_str(),
                "tmpfs",
                MountFlags::NOSUID | MountFlags::NODEV | MountFlags::NOEXEC,
                Some(data.as_c_str()),
            )?;
            lease.mounted = true;
            let fd = rustix::fs::openat(&lease.parent, lease.name.as_str(), DIR, Mode::empty())?;
            let fs = rustix::fs::fstatvfs(&fd)?;
            let required = rustix::fs::StatVfsMountFlags::NOSUID
                | rustix::fs::StatVfsMountFlags::NODEV
                | rustix::fs::StatVfsMountFlags::NOEXEC;
            if !fs.f_flag.contains(required)
                || fs.f_blocks.checked_mul(fs.f_frsize) != Some(size)
                || fs.f_files > 16
            {
                return Err(io::Error::other("capture mount budget readback"));
            }
            Ok(fd)
        })();
        match mount {
            Ok(fd) => Ok((lease, fd)),
            Err(e) => {
                let _ = lease.cleanup();
                Err(e)
            }
        }
    }
    pub(crate) fn cleanup(&mut self) -> io::Result<()> {
        if self.removed {
            return Ok(());
        }
        if self.mounted {
            rustix::mount::unmount(self.path.as_str(), UnmountFlags::empty())?;
            self.mounted = false;
        }
        rustix::fs::unlinkat(&self.parent, self.name.as_str(), AtFlags::REMOVEDIR)?;
        self.removed = true;
        Ok(())
    }
}
impl Drop for CaptureLease {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}
