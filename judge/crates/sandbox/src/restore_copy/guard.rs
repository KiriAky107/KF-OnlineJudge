//! Fixed production identity, stopped-writer and storage guards. No subprocesses.

use super::{Error, Result};
use rustix::process::{Resource, getrlimit};
use std::{collections::HashMap, fs, io::Read, os::unix::fs::MetadataExt, path::Path};

const CGROUP: &str = "/sys/fs/cgroup/kfonlinejudge.slice";
const BACKUPS: &str = "/var/lib/kf-onlinejudge/backups";

pub fn text(path: impl AsRef<Path>, bound: usize) -> Result<String> {
    let mut data = Vec::new();
    fs::File::open(path)
        .map_err(|_| Error::Guard)?
        .take(bound as u64 + 1)
        .read_to_end(&mut data)
        .map_err(|_| Error::Guard)?;
    if data.len() > bound {
        return Err(Error::Guard);
    }
    String::from_utf8(data).map_err(|_| Error::Guard)
}

pub fn stopped() -> Result<()> {
    for name in [
        "proxy",
        "api",
        "outbox",
        "worker",
        "problem-import",
        "coordinator",
        "sandbox-factory",
        "postgres",
        "rabbitmq",
    ] {
        let path = Path::new(CGROUP).join(format!("kf-{name}.service/cgroup.procs"));
        match fs::symlink_metadata(&path) {
            Ok(info) if info.is_file() && !info.file_type().is_symlink() => {
                if !text(path, 8192)?.trim().is_empty() {
                    return Err(Error::Guard);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            _ => return Err(Error::Guard),
        }
    }
    let mut count = 0;
    for entry in fs::read_dir("/proc").map_err(|_| Error::Guard)? {
        let entry = entry.map_err(|_| Error::Guard)?;
        if !entry
            .file_name()
            .as_encoded_bytes()
            .iter()
            .all(u8::is_ascii_digit)
        {
            continue;
        }
        count += 1;
        if count > 1024 {
            return Err(Error::Guard);
        }
        let path = entry.path().join("status");
        let raw = match text(&path, 16384) {
            Ok(v) => v,
            Err(_) if !path.exists() => continue,
            Err(e) => return Err(e),
        };
        let uid = raw
            .lines()
            .find_map(|s| s.strip_prefix("Uid:"))
            .ok_or(Error::Guard)?;
        let values: Vec<_> = uid.split_whitespace().map(str::parse::<u32>).collect();
        if values.len() != 4
            || values
                .iter()
                .any(|v| matches!(v, Ok(1200..=1205 | 100000) | Err(_)))
        {
            return Err(Error::Guard);
        }
    }
    for name in ["tasks", "identities", "captures"] {
        let path = Path::new("/var/lib/kf-judge").join(name);
        match fs::read_dir(&path) {
            Ok(mut entries) => {
                if entries.next().is_some() {
                    return Err(Error::Guard);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            _ => return Err(Error::Guard),
        }
    }
    match fs::symlink_metadata("/var/lib/kf-judge/factory/control.sock") {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        _ => Err(Error::Guard),
    }
}

pub fn require() -> Result<()> {
    let raw = text("/proc/self/status", 16384)?;
    let fields: HashMap<_, _> = raw.lines().filter_map(|s| s.split_once(':')).collect();
    for key in ["Uid", "Gid"] {
        if fields
            .get(key)
            .ok_or(Error::Guard)?
            .split_whitespace()
            .collect::<Vec<_>>()
            != ["0", "0", "0", "0"]
        {
            return Err(Error::Guard);
        }
    }
    if fields
        .get("Groups")
        .ok_or(Error::Guard)?
        .split_whitespace()
        .any(|v| v != "0")
        || fields.get("NoNewPrivs").ok_or(Error::Guard)?.trim() != "1"
    {
        return Err(Error::Guard);
    }
    for key in ["CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"] {
        if u64::from_str_radix(fields.get(key).ok_or(Error::Guard)?.trim(), 16) != Ok(15) {
            return Err(Error::Guard);
        }
    }
    if text("/proc/self/cgroup", 4096)?.trim() != "0::/kfonlinejudge.slice/kf-node-restore.service"
    {
        return Err(Error::Guard);
    }
    for (group, limits) in [
        (
            format!("{CGROUP}/kf-node-restore.service"),
            ["536870912", "0", "32", "200000 100000"],
        ),
        (
            CGROUP.to_owned(),
            ["4026531840", "0", "384", "200000 100000"],
        ),
    ] {
        for (key, expected) in ["memory.max", "memory.swap.max", "pids.max", "cpu.max"]
            .into_iter()
            .zip(limits)
        {
            if text(Path::new(&group).join(key), 128)?.trim() != expected {
                return Err(Error::Guard);
            }
        }
    }
    for (resource, maximum) in [
        (Resource::Core, 0),
        (Resource::Fsize, 256 * 1024 * 1024),
        (Resource::Nofile, 256),
        (Resource::Stack, 16 * 1024 * 1024),
    ] {
        let observed = getrlimit(resource);
        if observed.current != Some(maximum) || observed.maximum != Some(maximum) {
            return Err(Error::Guard);
        }
    }
    let metadata = fs::symlink_metadata(BACKUPS).map_err(|_| Error::Guard)?;
    if !metadata.is_dir()
        || metadata.uid() != 0
        || metadata.gid() != 0
        || metadata.mode() & 0o7777 != 0o700
    {
        return Err(Error::Guard);
    }
    let mounts = text("/proc/self/mountinfo", 256 * 1024)?;
    let rows: Vec<Vec<&str>> = mounts
        .lines()
        .map(|r| r.split_whitespace().collect())
        .collect();
    let found: Vec<_> = rows.iter().filter(|r| r.get(4) == Some(&BACKUPS)).collect();
    if found.len() != 1 {
        return Err(Error::Guard);
    }
    let row = found[0];
    let sep = row.iter().position(|v| *v == "-").ok_or(Error::Guard)?;
    if sep < 6
        || row.len() <= sep + 3
        || row[3] != "/"
        || row[sep + 1] != "ext4"
        || row[6..sep]
            .iter()
            .any(|v| v.starts_with("shared:") || v.starts_with("master:"))
        || rows.iter().filter(|r| r.get(2) == Some(&row[2])).count() != 1
    {
        return Err(Error::Guard);
    }
    let options: Vec<_> = row[5].split(',').chain(row[sep + 3].split(',')).collect();
    if ["ro", "nosuid", "nodev", "noexec"]
        .iter()
        .any(|v| !options.contains(v))
    {
        return Err(Error::Guard);
    }
    let info = rustix::fs::statvfs(BACKUPS).map_err(|_| Error::Guard)?;
    let bytes = info
        .f_frsize
        .checked_mul(info.f_blocks)
        .ok_or(Error::Guard)?;
    if !(16 * 1024_u64.pow(3) * 9 / 10..=16 * 1024_u64.pow(3)).contains(&bytes) {
        return Err(Error::Guard);
    }
    stopped()
}
