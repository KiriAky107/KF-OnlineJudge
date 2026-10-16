//! Fixed post-pivot proc/dev policy. No host mount/device path is request data.
use rustix::fs::{AtFlags, FileType, Mode, OFlags, StatVfsMountFlags};
use rustix::mount::MountFlags;
use std::io;

const DEV_BYTES: u64 = 64 * 1024;
const DEVICES: [(&str, u32); 4] = [("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)];

fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

/// The root role already proved single-threaded before pivot; a fresh proc
/// mount is needed before repeating that check. Only namespace PID 1 calls us.
pub(crate) fn install() -> io::Result<()> {
    if rustix::process::getpid().as_raw_pid() != 1 {
        return Err(denied("proc setup requires namespace init"));
    }
    rustix::mount::mount(
        "proc",
        "/proc",
        "proc",
        MountFlags::RDONLY | MountFlags::NOSUID | MountFlags::NODEV | MountFlags::NOEXEC,
        Some(c"hidepid=2,subset=pid"),
    )?;
    crate::launcher::entry::ensure_single_threaded()?;
    rustix::mount::mount(
        "tmpfs",
        "/dev",
        "tmpfs",
        MountFlags::NOSUID | MountFlags::NOEXEC,
        Some(c"size=65536,nr_inodes=16,mode=0755"),
    )?;
    let dev = rustix::fs::open(
        "/dev",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    // Single-threaded fresh init owns this process-wide umask change. Restore
    // it even when mknod fails; the caller terminates the namespace on error.
    let previous = rustix::process::umask(Mode::empty());
    let created = (|| {
        for (name, minor) in DEVICES {
            rustix::fs::mknodat(
                &dev,
                name,
                FileType::CharacterDevice,
                Mode::from_raw_mode(0o666),
                rustix::fs::makedev(1, minor),
            )?;
        }
        Ok::<_, io::Error>(())
    })();
    rustix::process::umask(previous);
    created?;
    drop(dev);
    // Read-only directory entries still permit I/O on the fixed character
    // devices. NODEV must remain absent here; all other mounts retain NODEV.
    rustix::mount::mount_remount(
        "/dev",
        MountFlags::RDONLY | MountFlags::NOSUID | MountFlags::NOEXEC,
        c"",
    )?;
    Ok(())
}

pub(crate) fn verify(pid: u32) -> io::Result<()> {
    let base = format!("/proc/{pid}/root");
    let proc = rustix::fs::open(
        format!("{base}/proc"),
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let required =
        StatVfsMountFlags::RDONLY | StatVfsMountFlags::NOSUID | StatVfsMountFlags::NOEXEC;
    if rustix::fs::fstatfs(&proc)?.f_type != rustix::fs::PROC_SUPER_MAGIC
        || !rustix::fs::fstatvfs(&proc)?
            .f_flag
            .contains(required | StatVfsMountFlags::NODEV)
    {
        return Err(denied("restricted proc mount mismatch"));
    }
    let status = std::fs::read_to_string(format!("{base}/proc/1/status"))?;
    if !status.lines().any(|line| line == "Pid:\t1")
        || !status.lines().any(|line| line == "PPid:\t0")
    {
        return Err(denied("proc is not rooted in namespace PID 1"));
    }
    let proc_mountinfo = std::fs::read_to_string(format!("{base}/proc/1/mountinfo"))?;
    let options = proc_mountinfo
        .lines()
        .find_map(|line| {
            let (left, right) = line.split_once(" - ")?;
            (left.split_whitespace().nth(4) == Some("/proc"))
                .then(|| right.split_whitespace().nth(2))
                .flatten()
        })
        .ok_or_else(|| denied("missing proc mount options"))?;
    verify_proc_options(options)?;
    for path in [
        "sys",
        "sysrq-trigger",
        "kcore",
        "keys",
        "meminfo",
        "cpuinfo",
    ] {
        match rustix::fs::statat(&proc, path, AtFlags::SYMLINK_NOFOLLOW) {
            Err(rustix::io::Errno::NOENT) => {}
            _ => return Err(denied("global proc entry exposed")),
        }
    }
    let dev = rustix::fs::open(
        format!("{base}/dev"),
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let stat = rustix::fs::fstatvfs(&dev)?;
    if rustix::fs::fstatfs(&dev)?.f_type as u64 != 0x0102_1994
        || stat.f_blocks.checked_mul(stat.f_frsize) != Some(DEV_BYTES)
        || !stat.f_flag.contains(required)
        || stat.f_flag.contains(StatVfsMountFlags::NODEV)
    {
        return Err(denied("restricted dev mount mismatch"));
    }
    let mut entries = std::fs::read_dir(format!("{base}/dev"))?
        .map(|entry| entry.map(|e| e.file_name()))
        .collect::<io::Result<Vec<_>>>()?;
    entries.sort();
    let mut expected: Vec<_> = DEVICES
        .iter()
        .map(|(name, _)| std::ffi::OsString::from(name))
        .collect();
    expected.sort();
    if entries != expected {
        return Err(denied("unexpected dev entry"));
    }
    for (name, minor) in DEVICES {
        let stat = rustix::fs::statat(&dev, name, AtFlags::SYMLINK_NOFOLLOW)?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::CharacterDevice
            || stat.st_rdev != rustix::fs::makedev(1, minor)
            || stat.st_uid != 0
            || stat.st_gid != 0
            || stat.st_mode & 0o7777 != 0o666
        {
            return Err(denied("device type/identity/mode mismatch"));
        }
    }
    Ok(())
}

fn verify_proc_options(options: &str) -> io::Result<()> {
    let hide: Vec<_> = options
        .split(',')
        .filter(|s| s.starts_with("hidepid="))
        .collect();
    let subset: Vec<_> = options
        .split(',')
        .filter(|s| s.starts_with("subset="))
        .collect();
    // Kernel show_options renders HIDEPID_INVISIBLE as a named value, even
    // when mount received the numeric value. Accept only this exact policy.
    if !matches!(hide.as_slice(), ["hidepid=2"] | ["hidepid=invisible"])
        || !matches!(subset.as_slice(), ["subset=pid"])
    {
        return Err(denied("proc restriction options mismatch"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::verify_proc_options;
    #[test]
    fn kernel_numeric_and_named_invisible_policy_are_equivalent() {
        for options in ["rw,hidepid=2,subset=pid", "rw,hidepid=invisible,subset=pid"] {
            assert!(verify_proc_options(options).is_ok());
        }
    }
    #[test]
    fn weaker_missing_or_ambiguous_proc_options_are_rejected() {
        for options in [
            "rw,subset=pid",
            "rw,hidepid=invisible",
            "rw,hidepid=off,subset=pid",
            "rw,hidepid=noaccess,subset=pid",
            "rw,hidepid=ptraceable,subset=pid",
            "rw,hidepid=2,hidepid=off,subset=pid",
            "rw,hidepid=2,subset=pid,subset=all",
        ] {
            assert!(verify_proc_options(options).is_err(), "{options}");
        }
    }
}
