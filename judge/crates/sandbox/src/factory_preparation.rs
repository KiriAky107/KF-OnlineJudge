//! Explicit fixed-node delegation preparation, separate from execution permits.
//! Only Sandbox owns these privileged kernel operations.

use crate::{cgroup, execution, factory};
use rustix::fs::{FlockOperation, Mode};
use serde::{Deserialize, Serialize};
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

const NODE: &str = "/sys/fs/cgroup/kfonlinejudge.slice";
const CHILD: &str = "kf-judge-tasks";
const FACTORY: &str = "/sys/fs/cgroup/kfonlinejudge.slice/kf-sandbox-factory.service";

#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct DelegationRecord {
    schema_version: u8,
    boot_id: String,
    device: u64,
    inode: u64,
}

fn denied() -> io::Error {
    io::Error::new(
        io::ErrorKind::PermissionDenied,
        "factory preparation refused",
    )
}

fn empty(path: &Path) -> io::Result<()> {
    match std::fs::symlink_metadata(path) {
        Ok(_) => {
            crate::isolation::root::open_directory(path)?;
            if std::fs::read_dir(path)?.next().transpose()?.is_some() {
                return Err(denied());
            }
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

pub fn prepare(config_path: &Path) -> io::Result<serde_json::Value> {
    factory::clean_environment()?;
    let config = execution::load_config(config_path)?;
    execution::authorize_registry(&config)?;
    let policy = config.config();
    if policy.state_root != "/var/lib/kf-judge"
        || policy.tasks_root != "/var/lib/kf-judge/tasks"
        || policy.delegated_cgroup_root != format!("{NODE}/{CHILD}")
        || policy.identities.worker_uid != 1202
        || policy.identities.worker_gid != 1202
        || policy.identities.first_task_uid != 100000
        || policy.identities.first_task_gid != 100000
        || policy.identities.count != 1
        || policy.max_concurrent_runs != 1
    {
        return Err(denied());
    }
    let own = std::fs::read_to_string("/proc/self/cgroup")?;
    if own.trim() != format!("0::{}", FACTORY.trim_start_matches("/sys/fs/cgroup")) {
        return Err(denied());
    }
    let supervisor = crate::isolation::root::open_directory(Path::new(FACTORY))?;
    if cgroup::read(&supervisor, "cgroup.procs")?.trim() != std::process::id().to_string() {
        return Err(denied());
    }
    // A helper will later apply its own required flags. Inherited filtering or
    // no-new-privileges is not silently treated as a working execution gate.
    let status = std::fs::read_to_string("/proc/self/status")?;
    if !status.lines().any(|s| {
        s.strip_prefix("NoNewPrivs:")
            .is_some_and(|v| v.trim() == "0")
    }) {
        return Err(denied());
    }
    let node = crate::isolation::root::open_directory(Path::new(NODE))?;
    if rustix::fs::fstatfs(&node)?.f_type as u64 != 0x6367_7270
        || cgroup::read(&node, "cgroup.type")?.trim() != "domain"
        || !cgroup::read(&node, "cgroup.procs")?.trim().is_empty()
    {
        return Err(denied());
    }
    for (name, expected) in [
        ("memory.max", "4026531840"),
        ("memory.swap.max", "0"),
        ("pids.max", "384"),
        ("cpu.max", "200000 100000"),
    ] {
        if cgroup::read(&node, name)?.trim() != expected {
            return Err(denied());
        }
    }
    let controllers = cgroup::read(&node, "cgroup.subtree_control")?;
    if !["cpu", "memory", "pids"]
        .iter()
        .all(|c| controllers.split_whitespace().any(|v| v == *c))
    {
        return Err(denied());
    }
    for name in ["tasks", "identities", "captures"] {
        empty(&Path::new(&policy.state_root).join(name))?;
    }
    let root = Path::new(&policy.state_root).join("factory");
    if !root.try_exists()? {
        std::fs::create_dir(&root)?;
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o711))?;
    }
    let root_fd = crate::isolation::root::open_directory(&root)?;
    let lock = factory::private_file(&root.join("lock"))?;
    rustix::fs::flock(&lock, FlockOperation::NonBlockingLockExclusive)?;
    match std::fs::symlink_metadata(root.join("control.sock")) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
        Ok(_) => return Err(denied()),
    }
    for name in ["tasks", "identities", "captures"] {
        empty(&Path::new(&policy.state_root).join(name))?;
    }
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?
        .trim()
        .to_owned();
    if boot.len() != 36 || !boot.bytes().all(|c| c.is_ascii_hexdigit() || c == b'-') {
        return Err(denied());
    }
    let record_path = root.join("delegation.json");
    let old = if record_path.try_exists()? {
        let mut file = factory::private_file(&record_path)?;
        if file.metadata()?.len() > 1024 {
            return Err(denied());
        }
        let mut bytes = Vec::new();
        (&mut file).take(1025).read_to_end(&mut bytes)?;
        let record: DelegationRecord = serde_json::from_slice(&bytes).map_err(|_| denied())?;
        if record.schema_version != 1 || record.boot_id.len() != 36 {
            return Err(denied());
        }
        Some(record)
    } else {
        if std::fs::symlink_metadata(&record_path).is_ok() {
            return Err(denied());
        }
        None
    };
    let delegated = Path::new(&policy.delegated_cgroup_root);
    let existed = delegated.try_exists()?;
    if existed {
        let fd = crate::isolation::root::open_directory(delegated)?;
        let st = rustix::fs::fstat(&fd)?;
        if old.as_ref()
            != Some(&DelegationRecord {
                schema_version: 1,
                boot_id: boot.clone(),
                device: st.st_dev,
                inode: st.st_ino,
            })
            || !cgroup::read(&fd, "cgroup.procs")?.trim().is_empty()
        {
            return Err(denied());
        }
        if std::fs::read_dir(delegated)?.any(|entry| {
            entry.map_or(true, |e| {
                e.file_type().map_or(true, |t| t.is_dir() || t.is_symlink())
            })
        }) {
            return Err(denied());
        }
    } else {
        if std::fs::symlink_metadata(delegated).is_ok()
            || old.as_ref().is_some_and(|record| record.boot_id == boot)
        {
            return Err(denied());
        }
        // A new boot may recreate an empty delegation only after the persistent
        // task/lease/capture checks above. Unexpected same-boot loss is refused.
        rustix::fs::mkdirat(&node, CHILD, Mode::from_raw_mode(0o700))?;
    }
    // Existing descriptor-based checks apply/read back aggregate ceilings,
    // no swap, controller availability and bounded depth/descendant counts.
    let _group = cgroup::DelegatedCgroup::open(&config).map_err(|_| denied())?;
    let fd = crate::isolation::root::open_directory(delegated)?;
    let st = rustix::fs::fstat(&fd)?;
    let record = DelegationRecord {
        schema_version: 1,
        boot_id: boot,
        device: st.st_dev,
        inode: st.st_ino,
    };
    let bytes = serde_json::to_vec(&record)?;
    let mut stored = factory::private_file(&record_path)?;
    stored.set_len(0)?;
    stored.write_all(&bytes)?;
    stored.sync_all()?;
    rustix::fs::fsync(&root_fd)?;
    let actual = ["memory.max", "memory.swap.max", "pids.max", "cpu.max"]
        .into_iter()
        .map(|name| Ok((name, cgroup::read(&fd, name)?.trim().to_owned())))
        .collect::<io::Result<std::collections::BTreeMap<_, _>>>()?;
    Ok(serde_json::json!({
        "delegation_prepared": true, "existing_owned_delegation": existed,
        "actual_limits": actual, "services_started": false, "isolation_verified": false,
    }))
}
