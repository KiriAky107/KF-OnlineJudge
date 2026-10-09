//! Fixed pre-exec read-only bindings. No task code exists during this stage.
use crate::spec::artifact_path;
use rustix::fd::OwnedFd;
use rustix::fs::{FileType, Mode, OFlags, ResolveFlags, SealFlags, StatVfsMountFlags};
use rustix::mount::MountFlags;
use std::io;

fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
const RESOLVE: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_SYMLINKS)
    .union(ResolveFlags::NO_MAGICLINKS)
    .union(ResolveFlags::NO_XDEV);
const DIR: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::CLOEXEC);

fn directory(parent: &OwnedFd, name: &str) -> io::Result<OwnedFd> {
    match rustix::fs::mkdirat(parent, name, Mode::from_raw_mode(0o1777)) {
        Ok(()) | Err(rustix::io::Errno::EXIST) => {}
        Err(error) => return Err(error.into()),
    }
    let next = rustix::fs::openat2(parent, name, DIR, Mode::empty(), RESOLVE)?;
    let stat = rustix::fs::fstat(&next)?;
    if stat.st_uid != 0 || stat.st_gid != 0 {
        return Err(denied("input parent owner"));
    }
    // Root-owned sticky directories protect input ancestors while permitting
    // the future task identity to create compiler output beside input files.
    rustix::fs::fchmod(&next, Mode::from_raw_mode(0o1777))?;
    Ok(next)
}

pub(crate) fn bind(relative: &str, fd: &OwnedFd, bytes: u64, executable: bool) -> io::Result<()> {
    super::super::launcher::entry::ensure_single_threaded()?;
    if rustix::process::getpid().as_raw_pid() != 1 || !rustix::process::geteuid().is_root() {
        return Err(denied("input binding requires privileged namespace init"));
    }
    artifact_path(relative, "input").map_err(|_| denied("input binding path"))?;
    let source = rustix::fs::fstat(fd)?;
    let seals =
        SealFlags::WRITE | SealFlags::GROW | SealFlags::SHRINK | SealFlags::EXEC | SealFlags::SEAL;
    if FileType::from_raw_mode(source.st_mode) != FileType::RegularFile
        || source.st_nlink != 0
        || u64::try_from(source.st_size).ok() != Some(bytes)
        || source.st_mode & 0o7777 != if executable { 0o555 } else { 0o444 }
        || !rustix::fs::fcntl_get_seals(fd)?.contains(seals)
        || !rustix::io::fcntl_getfd(fd)?.contains(rustix::io::FdFlags::CLOEXEC)
        || rustix::fs::fcntl_getfl(fd)? & OFlags::ACCMODE != OFlags::RDONLY
    {
        return Err(denied("unsealed or mismatched input descriptor"));
    }
    let mut parent = rustix::fs::open("/work", DIR | OFlags::NOFOLLOW, Mode::empty())?;
    for component in relative
        .split('/')
        .collect::<Vec<_>>()
        .split_last()
        .ok_or_else(|| denied("empty input path"))?
        .1
    {
        parent = directory(&parent, component)?;
    }
    let name = relative
        .rsplit('/')
        .next()
        .ok_or_else(|| denied("input basename"))?;
    let placeholder = rustix::fs::openat2(
        &parent,
        name,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
        RESOLVE,
    )?;
    // Anonymous memfd mounts are internal to the kernel and cannot be bound
    // into this mount namespace (EINVAL). Copy the sealed source into the
    // already-bounded work tmpfs before making a read-only self-bind. No task
    // code runs and no writable handle survives the acknowledgement.
    let mut buffer = [0_u8; 65536];
    let mut offset = 0_u64;
    while offset < bytes {
        let length = usize::try_from((bytes - offset).min(buffer.len() as u64)).unwrap();
        let count = rustix::io::pread(fd, &mut buffer[..length], offset)?;
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "sealed input EOF",
            ));
        }
        let mut written = 0;
        while written < count {
            let n = rustix::io::pwrite(
                &placeholder,
                &buffer[written..count],
                offset + written as u64,
            )?;
            if n == 0 {
                return Err(io::Error::new(io::ErrorKind::WriteZero, "input copy"));
            }
            written += n;
        }
        offset += count as u64;
    }
    rustix::fs::fchmod(
        &placeholder,
        Mode::from_raw_mode(if executable { 0o555 } else { 0o444 }),
    )?;
    drop(placeholder);
    // Target components were opened without links and are held in root-owned
    // directories. Only the single-threaded trusted helper/bootstrap exist;
    // the parent verified task membership before releasing this fixed stage.
    let target = format!("/work/{relative}");
    rustix::mount::mount_bind(&target, &target)?;
    let flags = MountFlags::BIND
        | MountFlags::RDONLY
        | MountFlags::NOSUID
        | MountFlags::NODEV
        | if executable {
            MountFlags::empty()
        } else {
            MountFlags::NOEXEC
        };
    rustix::mount::mount_remount(&target, flags, c"")?;
    Ok(())
}

pub(crate) fn finish() -> io::Result<()> {
    let work = rustix::fs::open("/work", DIR | OFlags::NOFOLLOW, Mode::empty())?;
    let stat = rustix::fs::fstat(&work)?;
    if stat.st_uid != 0 || stat.st_gid != 0 {
        return Err(denied("work owner"));
    }
    rustix::fs::fchmod(&work, Mode::from_raw_mode(0o1777))?;
    Ok(())
}

pub(crate) fn verify(pid: u32, inputs: &[crate::artifacts::PreparedInput]) -> io::Result<()> {
    let work = rustix::fs::open(format!("/proc/{pid}/root/work"), DIR, Mode::empty())?;
    let stat = rustix::fs::fstat(&work)?;
    if stat.st_uid != 0 || stat.st_gid != 0 || stat.st_mode & 0o7777 != 0o1777 {
        return Err(denied("work mode"));
    }
    for input in inputs {
        let target = rustix::fs::openat2(
            &work,
            input.path.as_str(),
            OFlags::RDONLY | OFlags::CLOEXEC,
            Mode::empty(),
            RESOLVE & !ResolveFlags::NO_XDEV,
        )?;
        let actual = rustix::fs::fstat(&target)?;
        let expected = rustix::fs::fstat(&input.file)?;
        let flags = rustix::fs::fstatvfs(&target)?.f_flag;
        if FileType::from_raw_mode(actual.st_mode) != FileType::RegularFile
            || actual.st_nlink != 1
            || actual.st_size != expected.st_size
            || actual.st_mode != expected.st_mode
            || actual.st_uid != expected.st_uid
            || actual.st_gid != expected.st_gid
            || !flags.contains(
                StatVfsMountFlags::RDONLY | StatVfsMountFlags::NOSUID | StatVfsMountFlags::NODEV,
            )
            || flags.contains(StatVfsMountFlags::NOEXEC) == input.executable
        {
            return Err(denied("input binding readback"));
        }
        // Compare fixed-sized chunks through the retained sealed source FD.
        // Neither a hash collision nor a pathname re-open can validate input.
        let mut source = [0_u8; 65536];
        let mut bound = [0_u8; 65536];
        let mut offset = 0_u64;
        while offset < input.file.bytes() {
            let length =
                usize::try_from((input.file.bytes() - offset).min(source.len() as u64)).unwrap();
            let a = rustix::io::pread(&input.file, &mut source[..length], offset)?;
            let b = rustix::io::pread(&target, &mut bound[..length], offset)?;
            if a == 0 || a != b || source[..a] != bound[..b] {
                return Err(denied("input content readback"));
            }
            offset += a as u64;
        }
    }
    // No input/directory capability is retained by PID 1 after acknowledgement.
    let mut fds = std::fs::read_dir(format!("/proc/{pid}/fd"))?
        .map(|entry| {
            entry.and_then(|e| {
                e.file_name()
                    .to_str()
                    .and_then(|s| s.parse::<i32>().ok())
                    .ok_or_else(|| denied("fd name"))
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    fds.sort_unstable();
    if fds != [0, 1, 2, 3] {
        return Err(denied("input descriptor leaked"));
    }
    Ok(())
}
