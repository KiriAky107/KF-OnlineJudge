//! Explicit stopped-factory ledger rotation. Historical IDs still reject replay.
//! No rotation is performed by startup, restart, expiry or allocation.

use crate::runtime::ValidatedRuntimeConfig;
use rustix::fs::FlockOperation;
use std::collections::HashSet;
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

pub(crate) const MAX_IDS: usize = 4096;
const MAX_CYCLES: usize = 64;
const MAX_LEDGER_BYTES: u64 = (MAX_IDS * 65) as u64;

fn denied() -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, "factory cycle refused")
}

pub(crate) fn parse_ids(bytes: &[u8]) -> io::Result<HashSet<String>> {
    if bytes.len() as u64 > MAX_LEDGER_BYTES {
        return Err(denied());
    }
    let text = std::str::from_utf8(bytes).map_err(|_| denied())?;
    if !text.is_empty() && !text.ends_with('\n') {
        return Err(denied());
    }
    let mut ids = HashSet::new();
    for id in text.lines() {
        if crate::spec::identifier(id, "run_id").is_err() || !ids.insert(id.to_owned()) {
            return Err(denied());
        }
    }
    if ids.len() > MAX_IDS {
        return Err(denied());
    }
    Ok(ids)
}

pub(crate) fn read_ledger(file: &mut File) -> io::Result<Vec<u8>> {
    if file.metadata()?.len() > MAX_LEDGER_BYTES {
        return Err(denied());
    }
    let mut bytes = Vec::new();
    file.take(MAX_LEDGER_BYTES + 1).read_to_end(&mut bytes)?;
    parse_ids(&bytes)?;
    Ok(bytes)
}

fn cycle_name(name: &str) -> io::Result<&str> {
    let id = name
        .strip_prefix("cycle-")
        .and_then(|s| s.strip_suffix(".requests"));
    match id {
        Some(id) if crate::spec::identifier(id, "cycle_id").is_ok() => Ok(id),
        _ => Err(denied()),
    }
}

fn archives(root: &Path) -> io::Result<Vec<PathBuf>> {
    let directory = root.join("cycles");
    if !directory.try_exists()? {
        if std::fs::symlink_metadata(&directory).is_ok() {
            return Err(denied());
        }
        return Ok(Vec::new());
    }
    crate::isolation::root::open_directory(&directory)?;
    if std::fs::metadata(&directory)?.mode() & 0o7777 != 0o700 {
        return Err(denied());
    }
    let mut paths = Vec::new();
    for entry in std::fs::read_dir(&directory)? {
        let entry = entry?;
        let name = entry.file_name();
        cycle_name(name.to_str().ok_or_else(denied)?)?;
        let info = std::fs::symlink_metadata(entry.path())?;
        if !info.is_file()
            || info.uid() != 0
            || info.gid() != 0
            || info.mode() & 0o7777 != 0o600
            || info.nlink() != 1
            || info.len() == 0
            || info.len() > MAX_LEDGER_BYTES
        {
            return Err(denied());
        }
        paths.push(entry.path());
        if paths.len() > MAX_CYCLES {
            return Err(denied());
        }
    }
    paths.sort();
    Ok(paths)
}

pub(crate) fn historical_ids(root: &Path) -> io::Result<HashSet<String>> {
    let mut all = HashSet::new();
    for path in archives(root)? {
        let mut file = crate::factory::private_file(&path)?;
        for id in parse_ids(&read_ledger(&mut file)?)? {
            if !all.insert(id) {
                return Err(denied());
            }
        }
    }
    Ok(all)
}

/// An operator uses a bounded root maintenance scope and the same protected
/// runtime/images. The factory lock and actual empty resources are mandatory.
/// An archived-but-not-truncated interruption requires repeating this explicit
/// operation with the same cycle ID; ordinary factory startup refuses it.
pub fn rotate(config_path: &Path, cycle_id: &str) -> io::Result<serde_json::Value> {
    crate::spec::identifier(cycle_id, "cycle_id").map_err(|_| denied())?;
    crate::factory::clean_environment()?;
    let config: ValidatedRuntimeConfig = crate::execution::load_config(config_path)?;
    crate::execution::authorize_registry(&config)?;
    let root = Path::new(&config.config().state_root).join("factory");
    crate::isolation::root::open_directory(&root)?;
    let lock = crate::factory::private_file(&root.join("lock"))?;
    rustix::fs::flock(&lock, FlockOperation::NonBlockingLockExclusive)?;
    if !crate::factory::drained(&config)?
        || std::fs::symlink_metadata(root.join("control.sock")).is_ok()
    {
        return Err(denied());
    }
    let directory = root.join("cycles");
    let old = archives(&root)?;
    let target = directory.join(format!("cycle-{cycle_id}.requests"));
    let exists = old.contains(&target);
    if !exists && old.len() == MAX_CYCLES {
        return Err(denied());
    }
    let mut ledger = crate::factory::private_file(&root.join("requests"))?;
    let bytes = read_ledger(&mut ledger)?;
    let current = parse_ids(&bytes)?;
    let mut previous = HashSet::new();
    let mut archived_target = None;
    for path in old {
        let mut file = crate::factory::private_file(&path)?;
        let archived = read_ledger(&mut file)?;
        if path == target {
            archived_target = Some(archived.clone());
        }
        for id in parse_ids(&archived)? {
            if !previous.insert(id.clone()) || (path != target && current.contains(&id)) {
                return Err(denied());
            }
        }
    }
    if let Some(archived) = archived_target {
        if !bytes.is_empty() && archived != bytes {
            return Err(denied());
        }
        // Empty current ledger means this explicit rotation already finished.
        // Its historical IDs remain intact and counted on the next startup.
    } else {
        if bytes.is_empty() {
            return Err(denied());
        }
        if !directory.try_exists()? {
            std::fs::create_dir(&directory)?;
            std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))?;
        }
        let fd = crate::isolation::root::open_directory(&directory)?;
        let mut archive = crate::factory::private_file(&target)?;
        if archive.metadata()?.len() != 0 {
            return Err(denied());
        }
        archive.write_all(&bytes)?;
        archive.sync_all()?;
        rustix::fs::fsync(&fd)?;
    }
    // Never erase a ledger before the retained copy is durable and matches.
    let mut archived = crate::factory::private_file(&target)?;
    let retained = read_ledger(&mut archived)?;
    if !bytes.is_empty() && retained != bytes {
        return Err(denied());
    }
    archived.sync_all()?;
    let archive_fd = crate::isolation::root::open_directory(&directory)?;
    rustix::fs::fsync(&archive_fd)?;
    let root_fd = crate::isolation::root::open_directory(&root)?;
    // Persist the newly created archive directory in its parent before the
    // active ledger can be truncated, including an explicit interrupted retry.
    rustix::fs::fsync(&root_fd)?;
    if !crate::factory::drained(&config)? {
        return Err(denied());
    }
    ledger.set_len(0)?;
    ledger.sync_all()?;
    rustix::fs::fsync(&root_fd)?;
    Ok(serde_json::json!({
        "cycle_id": cycle_id, "archived_requests": parse_ids(&retained)?.len(),
        "current_requests": 0, "historical_replay_ids_retained": true,
        "services_started": false, "isolation_verified": false,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ledger_parsing_keeps_unique_complete_bounded_identifiers() {
        assert!(parse_ids(b"").unwrap().is_empty());
        assert_eq!(parse_ids(b"run-one\nrun-two\n").unwrap().len(), 2);
        for bytes in [
            b"run-one".as_slice(),
            b"run-one\nrun-one\n",
            b"../foreign\n",
            b"\xff\n",
        ] {
            assert!(parse_ids(bytes).is_err());
        }
        assert!(parse_ids(&vec![b'x'; MAX_LEDGER_BYTES as usize + 1]).is_err());
    }

    #[test]
    fn cycle_names_cannot_choose_an_external_path() {
        assert_eq!(cycle_name("cycle-cycle-one.requests").unwrap(), "cycle-one");
        for name in [
            "requests",
            "cycle-../foreign.requests",
            "cycle-.requests",
            "cycle-one.tmp",
        ] {
            assert!(cycle_name(name).is_err());
        }
    }
}
