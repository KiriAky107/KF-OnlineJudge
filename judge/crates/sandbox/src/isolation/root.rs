//! Trusted image/task directory capabilities and fixed private root operations.
//! Request argv/paths never select a host mount source or target.

use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use rustix::fd::OwnedFd;
use rustix::fs::{AtFlags, Mode, OFlags, StatVfsMountFlags};
use rustix::mount::{MountFlags, UnmountFlags};
use std::ffi::CString;
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path};

const DIR: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
const MAX_STORAGE: u64 = 256 * 1024 * 1024;

#[derive(Clone, Copy, Debug)]
pub struct RootBudget {
    pub work_bytes: u64,
    pub tmp_bytes: u64,
}

impl RootBudget {
    pub(crate) fn for_spec(spec: &ValidatedSpec) -> io::Result<Self> {
        let total = spec.spec().limits.workdir_bytes / 4096 * 4096;
        let tmp_bytes = total / 4 / 4096 * 4096;
        let budget = Self {
            work_bytes: total - tmp_bytes,
            tmp_bytes,
        };
        if !budget.valid() {
            return Err(invalid("unsupported root storage budget"));
        }
        Ok(budget)
    }
    pub(crate) fn valid(&self) -> bool {
        self.work_bytes >= 4096
            && self.tmp_bytes >= 4096
            && self
                .work_bytes
                .checked_add(self.tmp_bytes)
                .is_some_and(|n| n <= MAX_STORAGE)
            && self.work_bytes.is_multiple_of(4096)
            && self.tmp_bytes.is_multiple_of(4096)
    }
}

pub(crate) struct RootDescriptors {
    pub image: OwnedFd,
    pub task: OwnedFd,
}

#[derive(Debug)]
pub(crate) struct RootError {
    pub source: io::Error,
    pub cleanup_error: Option<io::Error>,
}
impl From<io::Error> for RootError {
    fn from(source: io::Error) -> Self {
        Self {
            source,
            cleanup_error: None,
        }
    }
}
impl From<rustix::io::Errno> for RootError {
    fn from(source: rustix::io::Errno) -> Self {
        io::Error::from(source).into()
    }
}

pub(crate) struct RootLease {
    parent: OwnedFd,
    task: OwnedFd,
    image: OwnedFd,
    name: String,
    removed: bool,
    root_created: bool,
    pub budget: RootBudget,
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn safe_directory(fd: &OwnedFd) -> io::Result<()> {
    let stat = rustix::fs::fstat(fd)?;
    if rustix::fs::FileType::from_raw_mode(stat.st_mode) != rustix::fs::FileType::Directory
        || stat.st_uid != 0
        || stat.st_mode & 0o022 != 0
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "unsafe image/task directory owner or mode",
        ));
    }
    Ok(())
}
pub(crate) fn open_directory(path: &Path) -> io::Result<OwnedFd> {
    if !path.is_absolute() {
        return Err(invalid("root capability must be absolute"));
    }
    let mut fd = rustix::fs::open("/", DIR, Mode::empty())?;
    safe_directory(&fd)?;
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => {
                fd = rustix::fs::openat(&fd, name, DIR, Mode::empty())?;
                safe_directory(&fd)?;
            }
            _ => return Err(invalid("noncanonical root capability")),
        }
    }
    Ok(fd)
}
fn verify_anchors(image: &OwnedFd) -> io::Result<()> {
    // Sources contain directories rather than symlink mount targets. Image
    // publication/content immutability is an operator responsibility; artifact
    // TOCTOU/export validation is a separate, still-unimplemented stage.
    for name in ["work", "tmp", "proc", "dev"] {
        let fd = rustix::fs::openat(image, name, DIR, Mode::empty())?;
        safe_directory(&fd)?;
    }
    Ok(())
}

fn rebase_directory(fd: &OwnedFd) -> io::Result<OwnedFd> {
    use rustix::fd::AsRawFd;
    // Inherited FDs retain their original mount references across unshare.
    // Resolve the kernel-reported trusted path in the new namespace, walk it
    // without symlinks, and require the same inode before using it as a mount
    // target. No request controls this path or the root-owned ancestors.
    let path = std::fs::read_link(format!("/proc/self/fd/{}", fd.as_raw_fd()))?;
    let rebased = open_directory(&path)?;
    let a = rustix::fs::fstat(fd)?;
    let b = rustix::fs::fstat(&rebased)?;
    if a.st_dev != b.st_dev || a.st_ino != b.st_ino {
        return Err(invalid("directory capability identity changed"));
    }
    Ok(rebased)
}

impl RootLease {
    pub(crate) fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
    ) -> Result<Self, RootError> {
        spec.spec()
            .clone()
            .validate(&config.spec_policy())
            .map_err(|_| invalid("root request exceeds operator ceiling"))?;
        let budget = RootBudget::for_spec(spec)?;
        let image = open_directory(Path::new(&config.config().image_root))?;
        verify_anchors(&image)?;
        let parent = open_directory(Path::new(&config.config().tasks_root))?;
        let name = format!("kf-{}", spec.spec().run_id);
        rustix::fs::mkdirat(&parent, name.as_str(), Mode::from_raw_mode(0o700))?;
        let task = match rustix::fs::openat(&parent, name.as_str(), DIR, Mode::empty()) {
            Ok(fd) => fd,
            Err(source) => {
                return Err(RootError {
                    source: source.into(),
                    cleanup_error: rustix::fs::unlinkat(&parent, name.as_str(), AtFlags::REMOVEDIR)
                        .err()
                        .map(Into::into),
                });
            }
        };
        let mut lease = Self {
            parent,
            task,
            image,
            name,
            removed: false,
            root_created: false,
            budget,
        };
        if let Err(source) = (|| {
            safe_directory(&lease.task)?;
            rustix::fs::mkdirat(&lease.task, "root", Mode::from_raw_mode(0o700))?;
            lease.root_created = true;
            Ok::<_, io::Error>(())
        })() {
            return Err(RootError {
                source,
                cleanup_error: lease.cleanup().err(),
            });
        }
        Ok(lease)
    }
    pub(crate) fn descriptors(&self) -> io::Result<RootDescriptors> {
        Ok(RootDescriptors {
            image: rustix::io::fcntl_dupfd_cloexec(&self.image, 3)?,
            task: rustix::io::fcntl_dupfd_cloexec(&self.task, 3)?,
        })
    }
    pub(crate) fn verify_root(&self, pid: u32) -> io::Result<()> {
        let image = rustix::fs::fstat(&self.image)?;
        let actual = std::fs::metadata(format!("/proc/{pid}/root"))?;
        if actual.dev() != image.st_dev || actual.ino() != image.st_ino {
            return Err(invalid("actual root does not match leased image"));
        }
        let root = rustix::fs::open(
            format!("/proc/{pid}/root"),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        if !rustix::fs::fstatvfs(&root)?.f_flag.contains(
            StatVfsMountFlags::RDONLY | StatVfsMountFlags::NOSUID | StatVfsMountFlags::NODEV,
        ) {
            return Err(invalid("image root is not read-only"));
        }
        for (name, bytes, tmp) in [
            ("work", self.budget.work_bytes, false),
            ("tmp", self.budget.tmp_bytes, true),
        ] {
            let fd = rustix::fs::openat(&root, name, DIR, Mode::empty())?;
            let fs = rustix::fs::fstatvfs(&fd)?;
            let required = StatVfsMountFlags::NOSUID
                | StatVfsMountFlags::NODEV
                | if tmp {
                    StatVfsMountFlags::NOEXEC
                } else {
                    StatVfsMountFlags::empty()
                };
            if rustix::fs::fstatfs(&fd)?.f_type as u64 != 0x0102_1994
                || fs.f_blocks.checked_mul(fs.f_frsize) != Some(bytes)
                || !fs.f_flag.contains(required)
                || fs.f_flag.contains(StatVfsMountFlags::RDONLY)
            {
                return Err(invalid("scratch mount verification failed"));
            }
        }
        for entry in std::fs::read_dir(format!("/proc/{pid}/fd"))? {
            if std::fs::metadata(entry?.path())?.is_dir() {
                return Err(invalid("initialization directory FD leaked"));
            }
        }
        let cwd = std::fs::metadata(format!("/proc/{pid}/cwd"))?;
        let work = std::fs::metadata(format!("/proc/{pid}/root/work"))?;
        if (cwd.dev(), cwd.ino()) != (actual.dev(), actual.ino())
            && (cwd.dev(), cwd.ino()) != (work.dev(), work.ino())
        {
            return Err(invalid("initialization cwd is outside private root/work"));
        }
        Ok(())
    }
    pub(crate) fn cleanup(&mut self) -> io::Result<()> {
        if self.removed {
            return Ok(());
        }
        // No recursive deletion or adoption. All mounts/data exist only in the
        // helper's private namespace, which must be terminated before this.
        if self.root_created {
            rustix::fs::unlinkat(&self.task, "root", AtFlags::REMOVEDIR)?;
            self.root_created = false;
        }
        rustix::fs::unlinkat(&self.parent, self.name.as_str(), AtFlags::REMOVEDIR)?;
        self.removed = true;
        Ok(())
    }
}
impl Drop for RootLease {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

fn scratch(path: &str, bytes: u64, tmp: bool) -> io::Result<()> {
    let flags = MountFlags::NOSUID
        | MountFlags::NODEV
        | if tmp {
            MountFlags::NOEXEC
        } else {
            MountFlags::empty()
        };
    let data = CString::new(format!(
        "size={bytes},nr_inodes={},mode={}",
        (bytes / 4096).clamp(64, 65536),
        if tmp { "1777" } else { "0700" }
    ))
    .map_err(|_| invalid("scratch mount options"))?;
    rustix::mount::mount("tmpfs", path, "tmpfs", flags, Some(data.as_c_str()))?;
    Ok(())
}

/// Called only in the fresh PID-1 role after actual namespace verification.
pub(crate) fn install(budget: RootBudget) -> io::Result<()> {
    crate::launcher::entry::ensure_single_threaded()?;
    if !budget.valid() {
        return Err(invalid("invalid root storage budget"));
    }
    let image = rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stdout(), 3)?;
    let task = rustix::io::fcntl_dupfd_cloexec(rustix::stdio::stderr(), 3)?;
    safe_directory(&image)?;
    safe_directory(&task)?;
    verify_anchors(&image)?;
    let image = rebase_directory(&image)?;
    let task = rebase_directory(&task)?;
    let null = std::fs::File::options()
        .read(true)
        .write(true)
        .open("/dev/null")?;
    use rustix::fd::AsRawFd;
    let target = format!("/proc/self/fd/{}/root", task.as_raw_fd());
    // Nonrecursive bind deliberately excludes any source submounts. One image
    // bind is made read-only before adding our separate scratch mounts.
    rustix::mount::mount_bind(
        format!("/proc/self/fd/{}", image.as_raw_fd()),
        target.as_str(),
    )?;
    rustix::mount::mount_remount(
        target.as_str(),
        MountFlags::BIND | MountFlags::RDONLY | MountFlags::NOSUID | MountFlags::NODEV,
        "",
    )?;
    scratch(&format!("{target}/work"), budget.work_bytes, false)?;
    scratch(&format!("{target}/tmp"), budget.tmp_bytes, true)?;
    let root = rustix::fs::openat(&task, "root", DIR, Mode::empty())?;
    rustix::process::fchdir(&root)?;
    // Documented stacking form avoids modifying the immutable image to create
    // an old-root directory. Initial ramfs/rootfs remains unsupported/refused.
    rustix::process::pivot_root(".", ".")?;
    rustix::mount::unmount(".", UnmountFlags::DETACH)?;
    rustix::process::chdir("/")?;
    if !rustix::fs::statvfs("/")?
        .f_flag
        .contains(StatVfsMountFlags::RDONLY)
    {
        return Err(invalid("root read-only verification failed"));
    }
    // Release all image/task/old-root directory capabilities before reporting.
    rustix::stdio::dup2_stdout(&null)?;
    rustix::stdio::dup2_stderr(&null)?;
    drop(root);
    drop(task);
    drop(image);
    drop(null);
    rustix::process::chdir("/work")?;
    Ok(())
}
