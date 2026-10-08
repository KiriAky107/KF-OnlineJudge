//! Crash recovery of durable, root-owned task journals, never name-only adoption.
//! Only fixed descendants of operator-validated roots are eligible. Unknown,
//! replaced or incomplete resources remain reserved and produce a separate error.
use crate::{
    outcome::{ErrorCode, ExecutionError, ExecutionStage},
    runtime::ValidatedRuntimeConfig,
    spec::ValidatedSpec,
};
use rustix::{
    fd::{AsRawFd, OwnedFd},
    fs::{AtFlags, FlockOperation, Mode, OFlags},
};
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    io::{self, Read, Write},
    path::Path,
    time::{Duration, Instant},
};
const SERVICE: &str = "kf-sandbox-task-v2";
const MAX_RECORD: u64 = 8192;
const DIR: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
fn private(fd: &OwnedFd, mode: u32) -> io::Result<()> {
    let st = rustix::fs::fstat(fd)?;
    if st.st_uid != 0 || st.st_gid != 0 || st.st_mode & 0o7777 != mode {
        return Err(denied("unsafe recovery ownership/mode"));
    }
    Ok(())
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Inode {
    dev: u64,
    ino: u64,
}
impl Inode {
    fn of(fd: &OwnedFd) -> io::Result<Self> {
        let s = rustix::fs::fstat(fd)?;
        Ok(Self {
            dev: s.st_dev,
            ino: s.st_ino,
        })
    }
    fn check(&self, fd: &OwnedFd) -> io::Result<()> {
        if *self != Self::of(fd)? {
            return Err(denied("recorded resource inode changed"));
        }
        Ok(())
    }
}
#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Owner {
    boot_id: String,
    pid: u32,
    start_ticks: u64,
}
fn start_ticks(text: &str) -> io::Result<u64> {
    // comm may contain spaces and ')'; fields after its final ')' start at 3.
    text.rsplit_once(") ")
        .and_then(|(_, tail)| tail.split_whitespace().nth(19))
        .and_then(|n| n.parse().ok())
        .filter(|n| *n > 0)
        .ok_or_else(|| denied("invalid owner process start time"))
}
fn boot_id() -> io::Result<String> {
    let id = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?
        .trim()
        .to_owned();
    if id.len() != 36 || !id.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') {
        return Err(denied("invalid boot identity"));
    }
    Ok(id)
}
impl Owner {
    fn current() -> io::Result<Self> {
        Ok(Self {
            boot_id: boot_id()?,
            pid: std::process::id(),
            start_ticks: start_ticks(&std::fs::read_to_string("/proc/self/stat")?)?,
        })
    }
    fn alive(&self) -> io::Result<bool> {
        // A different kernel boot invalidates cgroup inode identity. Fail closed,
        // rather than treating a persistent record as authority over new groups.
        if self.boot_id != boot_id()? {
            return Err(denied("recovery record belongs to another kernel boot"));
        }
        match std::fs::read_to_string(format!("/proc/{}/stat", self.pid)) {
            Ok(s) => Ok(start_ticks(&s)? == self.start_ticks),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e),
        }
    }
}
#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Anchors {
    state: Inode,
    tasks: Inode,
    captures: Inode,
    cgroup: Inode,
}
struct Roots {
    state: OwnedFd,
    tasks: OwnedFd,
    captures: OwnedFd,
    cgroup: OwnedFd,
}
impl Roots {
    fn open(config: &ValidatedRuntimeConfig) -> io::Result<Self> {
        let c = config.config();
        let open = crate::isolation::root::open_directory;
        let state = open(Path::new(&c.state_root))?;
        let tasks = open(Path::new(&c.tasks_root))?;
        let captures = rustix::fs::openat(&state, "captures", DIR, Mode::empty())?;
        private(&captures, 0o700)?;
        let cgroup = open(Path::new(&c.delegated_cgroup_root))?;
        if rustix::fs::fstatfs(&cgroup)?.f_type as u64 != 0x6367_7270 {
            return Err(denied("recovery anchor is not cgroup v2"));
        }
        Ok(Self {
            state,
            tasks,
            captures,
            cgroup,
        })
    }
    fn anchors(&self) -> io::Result<Anchors> {
        Ok(Anchors {
            state: Inode::of(&self.state)?,
            tasks: Inode::of(&self.tasks)?,
            captures: Inode::of(&self.captures)?,
            cgroup: Inode::of(&self.cgroup)?,
        })
    }
}
#[derive(Clone, Copy)]
pub(crate) enum Resource {
    Task,
    Cgroup,
    Capture,
}
#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Resources {
    task: Option<Inode>,
    cgroup: Option<Inode>,
    capture: Option<Inode>,
}
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Record {
    service: String,
    version: u32,
    run_id: String,
    uid: u32,
    gid: u32,
    owner: Owner,
    anchors: Anchors,
    resources: Resources,
}
fn read_record(slot: &OwnedFd) -> io::Result<Record> {
    let fd = rustix::fs::openat(
        slot,
        "owner.json",
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    private(&fd, 0o600)?;
    let s = rustix::fs::fstat(&fd)?;
    if rustix::fs::FileType::from_raw_mode(s.st_mode) != rustix::fs::FileType::RegularFile
        || s.st_nlink != 1
        || s.st_size < 0
        || s.st_size as u64 > MAX_RECORD
    {
        return Err(denied("invalid journal file type/link/size"));
    }
    let mut bytes = Vec::new();
    // Inspect a path-only capability before reading: a foreign FIFO/device
    // named owner.json must never block or perform device I/O at startup.
    let reader = rustix::fs::open(
        format!("/proc/self/fd/{}", fd.as_raw_fd()),
        OFlags::RDONLY | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    File::from(reader)
        .take(MAX_RECORD + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_RECORD {
        return Err(denied("journal read bound"));
    }
    let record: Record =
        serde_json::from_slice(&bytes).map_err(|_| denied("unrecognized recovery record"))?;
    crate::spec::identifier(&record.run_id, "run_id")
        .map_err(|_| denied("invalid recovery task identifier"))?;
    if record.service != SERVICE
        || record.version != 2
        || record.owner.pid <= 1
        || record.owner.start_ticks == 0
    {
        return Err(denied("unrecognized recovery owner"));
    }
    Ok(record)
}
fn store(slot: &OwnedFd, record: &Record) -> io::Result<()> {
    let mut bytes = serde_json::to_vec(record)?;
    bytes.push(b'\n');
    if bytes.len() as u64 > MAX_RECORD {
        return Err(denied("journal write bound"));
    }
    let fd = rustix::fs::openat(
        slot,
        ".owner.next",
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
    )?;
    let mut file = File::from(fd);
    file.write_all(&bytes)?;
    file.sync_all()?;
    rustix::fs::renameat(slot, ".owner.next", slot, "owner.json")?;
    rustix::fs::fsync(slot)?;
    Ok(())
}
pub(crate) fn initialize_slot(
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    slot: &OwnedFd,
    uid: u32,
    gid: u32,
) -> io::Result<()> {
    rustix::fs::flock(slot, FlockOperation::NonBlockingLockExclusive)?;
    private(slot, 0o700)?;
    let state = crate::isolation::root::open_directory(Path::new(&config.config().state_root))?;
    match rustix::fs::mkdirat(&state, "captures", Mode::from_raw_mode(0o700)) {
        Ok(()) | Err(rustix::io::Errno::EXIST) => {}
        Err(e) => return Err(e.into()),
    }
    let record = Record {
        service: SERVICE.into(),
        version: 2,
        run_id: spec.spec().run_id.clone(),
        uid,
        gid,
        owner: Owner::current()?,
        anchors: Roots::open(config)?.anchors()?,
        resources: Resources::default(),
    };
    store(slot, &record)
}
/// Primitive-only tests have no identity lease. A full task records each inode
/// before that resource can enroll a user process or mount a capture filesystem.
pub(crate) fn register(
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    resource: Resource,
    fd: &OwnedFd,
) -> io::Result<()> {
    let state = crate::isolation::root::open_directory(Path::new(&config.config().state_root))?;
    let parent = match rustix::fs::openat(&state, "identities", DIR, Mode::empty()) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    private(&parent, 0o700)?;
    let current = Owner::current()?;
    for index in 0..config.config().max_concurrent_runs {
        let (uid, gid) = config
            .task_identity(index)
            .ok_or_else(|| denied("identity slot"))?;
        let slot = match rustix::fs::openat(&parent, format!("uid-{uid}"), DIR, Mode::empty()) {
            Ok(fd) => fd,
            Err(rustix::io::Errno::NOENT) => continue,
            Err(e) => return Err(e.into()),
        };
        private(&slot, 0o700)?;
        let Ok(mut record) = read_record(&slot) else {
            continue;
        };
        if record.owner != current || record.run_id != spec.spec().run_id {
            continue;
        }
        if record.uid != uid
            || record.gid != gid
            || record.anchors != Roots::open(config)?.anchors()?
        {
            return Err(denied("journal binding mismatch"));
        }
        let value = match resource {
            Resource::Task => &mut record.resources.task,
            Resource::Cgroup => &mut record.resources.cgroup,
            Resource::Capture => &mut record.resources.capture,
        };
        if value.is_some() {
            return Err(denied("resource already registered"));
        }
        *value = Some(Inode::of(fd)?);
        return store(&slot, &record);
    }
    Ok(())
}
pub(crate) fn verify_owned(
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    capture: bool,
) -> io::Result<()> {
    let roots = Roots::open(config)?;
    let parent = rustix::fs::openat(&roots.state, "identities", DIR, Mode::empty())?;
    private(&parent, 0o700)?;
    let current = Owner::current()?;
    for index in 0..config.config().max_concurrent_runs {
        let (uid, gid) = config
            .task_identity(index)
            .ok_or_else(|| denied("identity slot"))?;
        let slot = match rustix::fs::openat(&parent, format!("uid-{uid}"), DIR, Mode::empty()) {
            Ok(fd) => fd,
            Err(rustix::io::Errno::NOENT) => continue,
            Err(e) => return Err(e.into()),
        };
        private(&slot, 0o700)?;
        let Ok(record) = read_record(&slot) else {
            continue;
        };
        if record.owner == current
            && record.run_id == spec.spec().run_id
            && record.uid == uid
            && record.gid == gid
            && record.anchors == roots.anchors()?
            && record.resources.task.is_some()
            && record.resources.cgroup.is_some()
            && (!capture || record.resources.capture.is_some())
        {
            return Ok(());
        }
    }
    Err(denied("task has no complete durable recovery binding"))
}
fn names(fd: &OwnedFd) -> io::Result<Vec<String>> {
    let entries: Vec<_> = std::fs::read_dir(format!("/proc/self/fd/{}", fd.as_raw_fd()))?
        .take(257)
        .map(|e| {
            e?.file_name()
                .into_string()
                .map_err(|_| denied("non-UTF8 recovery entry"))
        })
        .collect::<io::Result<_>>()?;
    if entries.len() > 256 {
        return Err(denied("recovery directory bound"));
    }
    Ok(entries)
}
pub(crate) fn verify_release(slot: &OwnedFd) -> io::Result<()> {
    if names(slot)?
        .iter()
        .any(|n| n != "owner.json" && n != ".owner.next")
    {
        return Err(denied("unknown identity directory content"));
    }
    for name in names(slot)? {
        let fd = rustix::fs::openat(
            slot,
            name,
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        private(&fd, 0o600)?;
        let s = rustix::fs::fstat(&fd)?;
        if rustix::fs::FileType::from_raw_mode(s.st_mode) != rustix::fs::FileType::RegularFile
            || s.st_nlink != 1
            || s.st_size < 0
            || s.st_size as u64 > MAX_RECORD
        {
            return Err(denied("unknown journal replacement"));
        }
    }
    Ok(())
}
pub(crate) fn verify_released_resources(
    config: &ValidatedRuntimeConfig,
    slot: &OwnedFd,
    uid: u32,
    gid: u32,
) -> io::Result<()> {
    let record = read_record(slot)?;
    if record.owner != Owner::current()? || record.uid != uid || record.gid != gid {
        return Err(denied("identity owner record changed"));
    }
    let roots = Roots::open(config)?;
    if record.anchors != roots.anchors()? {
        return Err(denied("identity release anchors changed"));
    }
    // Reserve the UID until *all* fixed resource paths are gone. A failed
    // capture unmount/delete must not release an identity just because the
    // process group and task root have already been removed. O_PATH also
    // rejects a replacement symlink/special file without opening its target.
    let name = format!("kf-{}", record.run_id);
    for parent in [&roots.tasks, &roots.cgroup, &roots.captures] {
        match rustix::fs::openat(
            parent,
            name.as_str(),
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        ) {
            Err(rustix::io::Errno::NOENT) => {}
            Err(e) => return Err(e.into()),
            Ok(_) => return Err(denied("identity still owns a residual resource")),
        }
    }
    Ok(())
}
fn mount_id(fd: &OwnedFd) -> io::Result<u64> {
    std::fs::read_to_string(format!("/proc/self/fdinfo/{}", fd.as_raw_fd()))?
        .lines()
        .find_map(|s| s.strip_prefix("mnt_id:"))
        .and_then(|s| s.trim().parse().ok())
        .ok_or_else(|| denied("mount identity missing"))
}
fn same_mount(parent: &OwnedFd, fd: &OwnedFd) -> io::Result<()> {
    if mount_id(parent)? != mount_id(fd)? {
        return Err(denied("unexpected recovery submount"));
    }
    Ok(())
}
fn resource(parent: &OwnedFd, name: &str, inode: Option<Inode>) -> io::Result<Option<OwnedFd>> {
    let fd = match rustix::fs::openat(parent, name, DIR, Mode::empty()) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    inode
        .ok_or_else(|| denied("resource has no durable ownership binding"))?
        .check(&fd)?;
    private(&fd, 0o700)?;
    same_mount(parent, &fd)?;
    Ok(Some(fd))
}
fn remove_directory(parent: &OwnedFd, name: &str, held: &OwnedFd) -> io::Result<()> {
    let named = rustix::fs::openat(parent, name, DIR, Mode::empty())?;
    Inode::of(held)?.check(&named)?;
    same_mount(parent, &named)?;
    rustix::fs::unlinkat(parent, name, AtFlags::REMOVEDIR)?;
    Ok(())
}
fn wait_event(group: &OwnedFd, key: &str, value: &str, deadline: Instant) -> io::Result<()> {
    loop {
        if crate::cgroup::read(group, "cgroup.events")?
            .lines()
            .any(|s| s.split_once(' ') == Some((key, value)))
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "recovery group deadline",
            ));
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}
fn stop(group: &OwnedFd) -> io::Result<u32> {
    if rustix::fs::fstatfs(group)?.f_type as u64 != 0x6367_7270
        || crate::cgroup::read(group, "cgroup.type")?.trim() != "domain"
    {
        return Err(denied("invalid recovery task cgroup"));
    }
    for name in names(group)? {
        if rustix::fs::openat(group, name, DIR, Mode::empty()).is_ok() {
            return Err(denied("unexpected nested task cgroup"));
        }
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    crate::cgroup::write(group, "cgroup.freeze", "1")?;
    wait_event(group, "frozen", "1", deadline)?;
    let text = crate::cgroup::read(group, "cgroup.procs")?;
    let mut fds = Vec::new();
    for pid in text.split_whitespace() {
        if fds.len() >= 128 {
            return Err(denied("recovery process bound"));
        }
        let pid = pid
            .parse::<i32>()
            .ok()
            .and_then(rustix::process::Pid::from_raw)
            .ok_or_else(|| denied("invalid group member"))?;
        match rustix::process::pidfd_open(pid, rustix::process::PidfdFlags::empty()) {
            Ok(fd) => fds.push(fd),
            Err(rustix::io::Errno::SRCH) => {}
            Err(e) => return Err(e.into()),
        }
    }
    crate::cgroup::write(group, "cgroup.kill", "1")?;
    wait_event(group, "populated", "0", deadline)?;
    if !crate::cgroup::read(group, "cgroup.procs")?
        .trim()
        .is_empty()
    {
        return Err(denied("recovery group still populated"));
    }
    // Startup is not the parent of the old supervisor's descendants: do not
    // wait(-1) or signal numeric PIDs. Held pidfds prove exit; their actual
    // parent/system init owns zombie reaping, including across service restart.
    loop {
        let mut poll: Vec<_> = fds
            .iter()
            .map(|fd| rustix::event::PollFd::new(fd, rustix::event::PollFlags::IN))
            .collect();
        rustix::event::poll(
            &mut poll,
            Some(&rustix::event::Timespec {
                tv_sec: 0,
                tv_nsec: 0,
            }),
        )?;
        if poll.iter().all(|p| {
            p.revents()
                .intersects(rustix::event::PollFlags::IN | rustix::event::PollFlags::HUP)
        }) {
            return Ok(fds.len() as u32);
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "recovery pidfd exit deadline",
            ));
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}
#[derive(Debug, Serialize)]
pub struct RecoveryItem {
    pub uid: u32,
    pub run_id: Option<String>,
    pub status: &'static str,
    pub stopped_processes: u32,
    pub cleanup_error: Option<ExecutionError>,
}
#[derive(Debug, Default, Serialize)]
pub struct RecoveryReport {
    pub items: Vec<RecoveryItem>,
}
fn reclaim(
    roots: &Roots,
    identities: &OwnedFd,
    slot: &OwnedFd,
    name: &str,
    record: &Record,
    stopped: &mut u32,
) -> io::Result<()> {
    if record.anchors != roots.anchors()? {
        return Err(denied("recovery anchor binding changed"));
    }
    let task_name = format!("kf-{}", record.run_id);
    let group = resource(&roots.cgroup, &task_name, record.resources.cgroup)?;
    let task = resource(&roots.tasks, &task_name, record.resources.task)?;
    let capture = resource(&roots.captures, &task_name, record.resources.capture)?;
    *stopped = if let Some(group) = &group {
        stop(group)?
    } else {
        0
    };
    if let Some(group) = &group {
        remove_directory(&roots.cgroup, &task_name, group)?;
    }
    if let Some(task) = &task {
        let entries = names(task)?;
        if entries.iter().any(|n| n != "root") {
            return Err(denied("unknown task directory content"));
        }
        if entries.iter().any(|n| n == "root") {
            let root = rustix::fs::openat(task, "root", DIR, Mode::empty())?;
            private(&root, 0o700)?;
            same_mount(task, &root)?;
            if !names(&root)?.is_empty() {
                return Err(denied("unexpected task root contents"));
            }
            remove_directory(task, "root", &root)?;
        }
        remove_directory(&roots.tasks, &task_name, task)?;
    }
    if let Some(capture) = &capture {
        if !names(capture)?.is_empty() {
            return Err(denied("unexpected capture contents"));
        }
        remove_directory(&roots.captures, &task_name, capture)?;
    }
    verify_release(slot)?;
    for file in [".owner.next", "owner.json"] {
        match rustix::fs::unlinkat(slot, file, AtFlags::empty()) {
            Ok(()) | Err(rustix::io::Errno::NOENT) => {}
            Err(e) => return Err(e.into()),
        }
    }
    remove_directory(identities, name, slot)?;
    rustix::fs::fsync(identities)?;
    Ok(())
}
/// Call in the fresh privileged service-start helper, before accepting runs.
/// A live owner or held directory flock is skipped; failures retain the UID.
pub fn recover(config: &ValidatedRuntimeConfig) -> io::Result<RecoveryReport> {
    crate::launcher::entry::ensure_single_threaded()?;
    if !rustix::process::geteuid().is_root() {
        return Err(denied("privileged recovery helper required"));
    }
    let state = crate::isolation::root::open_directory(Path::new(&config.config().state_root))?;
    let identities = match rustix::fs::openat(&state, "identities", DIR, Mode::empty()) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(RecoveryReport::default()),
        Err(e) => return Err(e.into()),
    };
    private(&identities, 0o700)?;
    let roots = Roots::open(config)?;
    let mut report = RecoveryReport::default();
    for index in 0..config.config().max_concurrent_runs {
        let (uid, gid) = config
            .task_identity(index)
            .ok_or_else(|| denied("recovery identity mapping"))?;
        let name = format!("uid-{uid}");
        let slot = match rustix::fs::openat(&identities, name.as_str(), DIR, Mode::empty()) {
            Ok(fd) => fd,
            Err(rustix::io::Errno::NOENT) => continue,
            Err(e) => {
                report.items.push(RecoveryItem {
                    uid,
                    run_id: None,
                    status: "unrecognized",
                    stopped_processes: 0,
                    cleanup_error: Some(crate::launcher::blocked::failure(
                        ExecutionStage::Cleanup,
                        ErrorCode::CleanupIncomplete,
                        Some(&e.into()),
                    )),
                });
                continue;
            }
        };
        let mut item = RecoveryItem {
            uid,
            run_id: None,
            status: "unrecognized",
            stopped_processes: 0,
            cleanup_error: None,
        };
        let result = (|| {
            private(&slot, 0o700)?;
            match rustix::fs::flock(&slot, FlockOperation::NonBlockingLockExclusive) {
                Ok(()) => {}
                Err(rustix::io::Errno::WOULDBLOCK) => {
                    item.status = "active";
                    return Ok(());
                }
                Err(e) => return Err(e.into()),
            }
            let Ok(record) = read_record(&slot) else {
                return Ok(());
            };
            item.run_id = Some(record.run_id.clone());
            if record.uid != uid || record.gid != gid {
                return Err(denied("recorded identity mapping changed"));
            }
            if record.owner.alive()? {
                item.status = "active";
                return Ok(());
            }
            reclaim(
                &roots,
                &identities,
                &slot,
                &name,
                &record,
                &mut item.stopped_processes,
            )?;
            item.status = "reclaimed";
            Ok::<_, io::Error>(())
        })();
        if let Err(e) = result {
            item.status = "cleanup_failed";
            item.cleanup_error = Some(crate::launcher::blocked::failure(
                ExecutionStage::Cleanup,
                ErrorCode::CleanupIncomplete,
                Some(&e),
            ));
        }
        report.items.push(item);
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn owner_start_time_parses_parenthesized_comm_and_rejects_truncation() {
        let tail = (0..20)
            .map(|n| if n == 19 { "12345".into() } else { "1".into() })
            .collect::<Vec<String>>()
            .join(" ");
        assert_eq!(
            start_ticks(&format!("7 (name ) has spaces) {tail}")).unwrap(),
            12345
        );
        for bad in ["", "7 (name) S 1", "7 name S 1"] {
            assert!(start_ticks(bad).is_err());
        }
    }
}
