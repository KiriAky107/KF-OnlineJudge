//! File capabilities and immutable, bounded snapshots, entirely inside sandbox.
//!
//! Request values are logical IDs/relative paths. A trusted artifact provider
//! supplies an owned directory FD; no request can select a host directory.
//! Outputs are resolved only through a verified, frozen target's work FD.
//! Snapshotting is not publication, input mounting, stdio wiring or an exec permit.

use crate::spec::{ValidatedSpec, artifact_path};
use rustix::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd};
use rustix::fs::{FileType, MemfdFlags, Mode, OFlags, ResolveFlags, SealFlags, Stat};
use std::fs::File;
use std::io::{self, Read, Write};

/// Per-call operator ceiling in addition to the request's file/work limits.
/// The supervisor must also bound concurrent preparation reservations.
pub const MAX_SNAPSHOT_BYTES: u64 = 64 * 1024 * 1024;
const SEALS: SealFlags = SealFlags::WRITE
    .union(SealFlags::SHRINK)
    .union(SealFlags::GROW)
    .union(SealFlags::EXEC)
    .union(SealFlags::SEAL);
const RESOLVE: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_SYMLINKS)
    .union(ResolveFlags::NO_MAGICLINKS)
    .union(ResolveFlags::NO_XDEV);

fn refused(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

/// Sealed backing inode and read-only CLOEXEC handle. No writable handle escapes.
pub struct SnapshotFile {
    file: File,
    bytes: u64,
}

impl SnapshotFile {
    pub(crate) fn from_received(fd: OwnedFd, bytes: u64) -> io::Result<Self> {
        let st = rustix::fs::fstat(&fd)?;
        if FileType::from_raw_mode(st.st_mode) != FileType::RegularFile
            || st.st_size < 0
            || st.st_size as u64 != bytes
            || bytes > MAX_SNAPSHOT_BYTES
            || rustix::fs::fcntl_getfl(&fd)?.intersection(OFlags::ACCMODE) != OFlags::RDONLY
            || !rustix::fs::fcntl_get_seals(&fd)?.contains(SEALS)
            || !rustix::io::fcntl_getfd(&fd)?.contains(rustix::io::FdFlags::CLOEXEC)
        {
            return Err(refused("invalid received snapshot"));
        }
        Ok(Self {
            file: File::from(fd),
            bytes,
        })
    }
    pub fn bytes(&self) -> u64 {
        self.bytes
    }
    pub(crate) fn from_capture(capture: &crate::io::Capture) -> io::Result<Self> {
        let bytes = capture.status().retained;
        if bytes > MAX_SNAPSHOT_BYTES {
            return Err(refused("capture snapshot ceiling"));
        }
        let mut target = File::from(rustix::fs::memfd_create(
            "kf-capture",
            MemfdFlags::CLOEXEC | MemfdFlags::ALLOW_SEALING | MemfdFlags::NOEXEC_SEAL,
        )?);
        let mut offset = 0;
        let mut buffer = [0_u8; 4096];
        while offset < bytes {
            let n = capture.read_at(&mut buffer, offset)?;
            if n == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "capture snapshot",
                ));
            }
            target.write_all(&buffer[..n])?;
            offset += n as u64;
        }
        rustix::fs::fchmod(&target, Mode::from_raw_mode(0o444))?;
        rustix::fs::fcntl_add_seals(&target, SEALS)?;
        if !rustix::fs::fcntl_get_seals(&target)?.contains(SEALS) {
            return Err(refused("capture seals"));
        }
        Ok(Self {
            file: readonly_handle(&target)?,
            bytes,
        })
    }
    /// A fresh cursor, retaining the same immutable inode (not a path lookup).
    pub fn reader(&self) -> io::Result<File> {
        readonly_handle(&self.file)
    }
}

impl AsFd for SnapshotFile {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.file.as_fd()
    }
}

pub struct PreparedInput {
    pub path: String,
    pub executable: bool,
    pub file: SnapshotFile,
}

pub struct PreparedInputs {
    pub files: Vec<PreparedInput>,
    /// Kept separate: stdin data is not implicitly exposed as a /work file.
    pub stdin: Option<SnapshotFile>,
}

pub struct OutputSnapshot {
    pub path: String,
    pub file: SnapshotFile,
}

/// The provider is responsible for selecting the task's authorized publication
/// directory. Ownership/mode checks do not authenticate artifact business IDs.
pub struct ArtifactRoot {
    fd: OwnedFd,
    owner: u32,
}

impl AsFd for ArtifactRoot {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.fd.as_fd()
    }
}
impl ArtifactRoot {
    pub fn open(path: &std::path::Path) -> io::Result<Self> {
        Self::from_directory(rustix::fs::open(
            path,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?)
    }
    pub fn snapshot_artifact(&self, id: &str, limit: u64) -> io::Result<SnapshotFile> {
        if limit == 0 || limit > MAX_SNAPSHOT_BYTES {
            return Err(refused("snapshot ceiling"));
        }
        let mut budget = SnapshotBudget {
            file_bytes: limit,
            remaining_bytes: limit,
        };
        self.snapshot_id(id, &mut budget, false)
    }
    pub(crate) fn from_worker_directory(fd: OwnedFd, owner: u32) -> io::Result<Self> {
        if owner < 1000 {
            return Err(refused("ordinary publication owner"));
        }
        Self::with_owner(fd, owner)
    }
    pub fn from_directory(fd: OwnedFd) -> io::Result<Self> {
        Self::with_owner(fd, rustix::process::geteuid().as_raw())
    }
    fn with_owner(fd: OwnedFd, owner: u32) -> io::Result<Self> {
        let stat = rustix::fs::fstat(&fd)?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::Directory
            || stat.st_uid != owner
            || stat.st_mode & 0o022 != 0
            || !rustix::io::fcntl_getfd(&fd)?.contains(rustix::io::FdFlags::CLOEXEC)
        {
            return Err(refused("untrusted artifact directory capability"));
        }
        Ok(Self { fd, owner })
    }

    pub fn snapshot_inputs(&self, spec: &ValidatedSpec) -> io::Result<PreparedInputs> {
        let directory = rustix::fs::fstat(&self.fd)?;
        if directory.st_uid != self.owner || directory.st_mode & 0o022 != 0 {
            return Err(refused("artifact directory authority changed"));
        }
        let request = spec.spec();
        let mut budget = SnapshotBudget::for_spec(spec);
        let mut files = Vec::with_capacity(request.inputs.len());
        for input in &request.inputs {
            // Validated IDs are a single component, so no untrusted ancestor
            // directory can substitute a differently owned publication tree.
            let executable = request.argv[0] == format!("/work/{}", input.path);
            let file = self.snapshot_id(&input.artifact_id, &mut budget, executable)?;
            files.push(PreparedInput {
                path: input.path.clone(),
                executable,
                file,
            });
        }
        let stdin = request
            .stdin_artifact
            .as_ref()
            .map(|id| self.snapshot_id(id, &mut budget, false))
            .transpose()?;
        Ok(PreparedInputs { files, stdin })
    }

    fn snapshot_id(
        &self,
        id: &str,
        budget: &mut SnapshotBudget,
        executable: bool,
    ) -> io::Result<SnapshotFile> {
        if id.is_empty()
            || id.len() > 64
            || !id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        {
            return Err(refused("invalid artifact identifier"));
        }
        snapshot(&self.fd, id, budget, Some(self.owner), executable)
    }
}

pub(crate) struct OutputPlan {
    paths: Vec<String>,
    budget: SnapshotBudget,
    java_classes: bool,
}

impl OutputPlan {
    pub(crate) fn for_spec(spec: &ValidatedSpec) -> Self {
        Self {
            paths: spec.spec().outputs.clone(),
            budget: SnapshotBudget::for_spec(spec),
            java_classes: spec.spec().profile == crate::spec::ExecutionProfile::JavaCompile,
        }
    }

    pub(crate) fn collect(&self, work: &OwnedFd) -> io::Result<Vec<OutputSnapshot>> {
        if FileType::from_raw_mode(rustix::fs::fstat(work)?.st_mode) != FileType::Directory
            || self.paths.len() > 16
        {
            return Err(refused("invalid output directory or count"));
        }
        let paths = if self.java_classes {
            let mut paths = Vec::new();
            let mut count = 0;
            let mut unsupported = false;
            // The entire group is frozen and drained. Discover only bounded,
            // flat compiler outputs through the held private work directory.
            for entry in std::fs::read_dir(format!("/proc/self/fd/{}", work.as_raw_fd()))? {
                count += 1;
                if count > kf_contracts::judging_v2::MAX_CLASS_FILES + 1 {
                    return Err(refused("class export count"));
                }
                let name = entry?
                    .file_name()
                    .into_string()
                    .map_err(|_| refused("class export name"))?;
                if name == "Main.java" {
                    continue;
                }
                if !kf_contracts::judging_v2::class_name(&name) {
                    // javac accepts packages and Unicode class names outside
                    // this protocol's flat ASCII class set. Suppress the whole
                    // batch so Worker reports unsupported entry as user CE.
                    // Links/special files remain infrastructure refusals and
                    // no unrecognized output is ever opened for reading.
                    let fd = rustix::fs::openat(
                        work,
                        name.as_str(),
                        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                        Mode::empty(),
                    )?;
                    let st = rustix::fs::fstat(&fd)?;
                    if st.st_dev != rustix::fs::fstat(work)?.st_dev
                        || !matches!(
                            FileType::from_raw_mode(st.st_mode),
                            FileType::RegularFile | FileType::Directory
                        )
                        || (FileType::from_raw_mode(st.st_mode) == FileType::RegularFile
                            && st.st_nlink != 1)
                    {
                        return Err(refused("class export path/type"));
                    }
                    unsupported = true;
                    continue;
                }
                paths.push(name);
            }
            if unsupported {
                return Ok(Vec::new());
            }
            paths.sort();
            paths
        } else {
            self.paths.clone()
        };
        let mut budget = self.budget;
        let mut files = Vec::with_capacity(paths.len());
        for path in &paths {
            let file = snapshot(work, path, &mut budget, None, false)?;
            if self.java_classes && file.bytes() == 0 {
                return Err(refused("empty class export"));
            }
            files.push(OutputSnapshot {
                path: path.clone(),
                file,
            });
        }
        // Errors drop every partial FD; there is no partially published batch.
        Ok(files)
    }
}

#[derive(Clone, Copy)]
struct SnapshotBudget {
    file_bytes: u64,
    remaining_bytes: u64,
}

impl SnapshotBudget {
    fn for_spec(spec: &ValidatedSpec) -> Self {
        Self {
            file_bytes: spec.spec().limits.file_bytes,
            remaining_bytes: spec.spec().limits.workdir_bytes.min(MAX_SNAPSHOT_BYTES),
        }
    }
}

fn regular(stat: &Stat, owner: Option<u32>) -> io::Result<u64> {
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
        || stat.st_nlink != 1
        || stat.st_mode & 0o7000 != 0
        || owner.is_some_and(|uid| stat.st_uid != uid || stat.st_mode & 0o022 != 0)
    {
        return Err(refused("artifact type, links, mode or ownership"));
    }
    u64::try_from(stat.st_size).map_err(|_| refused("invalid artifact size"))
}

fn unchanged(a: &Stat, b: &Stat) -> bool {
    a.st_dev == b.st_dev
        && a.st_ino == b.st_ino
        && a.st_mode == b.st_mode
        && a.st_uid == b.st_uid
        && a.st_gid == b.st_gid
        && a.st_nlink == b.st_nlink
        && a.st_size == b.st_size
        && a.st_mtime == b.st_mtime
        && a.st_mtime_nsec == b.st_mtime_nsec
        && a.st_ctime == b.st_ctime
        && a.st_ctime_nsec == b.st_ctime_nsec
}

fn readonly_handle(fd: impl AsFd) -> io::Result<File> {
    // This is the only deliberate magic-link lookup. The numeric FD is owned
    // and held throughout; it was already checked as a regular inode. Never
    // reopen an untrusted pathname after checking it. NOCTTY/NONBLOCK are
    // defense in depth; O_PATH validation occurs before device/FIFO open.
    let reopened = rustix::fs::open(
        format!("/proc/self/fd/{}", fd.as_fd().as_raw_fd()),
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NONBLOCK | OFlags::NOCTTY,
        Mode::empty(),
    )?;
    let before = rustix::fs::fstat(&fd)?;
    let after = rustix::fs::fstat(&reopened)?;
    if before.st_dev != after.st_dev || before.st_ino != after.st_ino {
        return Err(refused("reopened artifact identity"));
    }
    Ok(File::from(reopened))
}

fn snapshot(
    directory: &OwnedFd,
    relative: &str,
    budget: &mut SnapshotBudget,
    owner: Option<u32>,
    executable: bool,
) -> io::Result<SnapshotFile> {
    artifact_path(relative, "artifact").map_err(|_| refused("noncanonical artifact path"))?;
    // Atomic containment; reject unsupported openat2 or races, no fallback or
    // retry. O_PATH inspects special files without opening their device/pipe.
    let fd = rustix::fs::openat2(
        directory,
        relative,
        OFlags::PATH | OFlags::CLOEXEC,
        Mode::empty(),
        RESOLVE,
    )?;
    let before = rustix::fs::fstat(&fd)?;
    let bytes = regular(&before, owner)?;
    if bytes > budget.file_bytes || bytes > budget.remaining_bytes {
        return Err(refused("artifact byte budget"));
    }
    let mut source = readonly_handle(&fd)?;
    if !unchanged(&before, &rustix::fs::fstat(&source)?) {
        return Err(refused("artifact changed before copy"));
    }
    let mut target = File::from(rustix::fs::memfd_create(
        "kf-artifact",
        MemfdFlags::CLOEXEC
            | MemfdFlags::ALLOW_SEALING
            | if executable {
                MemfdFlags::EXEC
            } else {
                MemfdFlags::NOEXEC_SEAL
            },
    )?);
    let mut remaining = bytes;
    let mut buffer = [0_u8; 64 * 1024];
    while remaining != 0 {
        let length = usize::try_from(remaining.min(buffer.len() as u64))
            .map_err(|_| refused("artifact copy length"))?;
        source.read_exact(&mut buffer[..length])?;
        target.write_all(&buffer[..length])?;
        remaining -= length as u64;
    }
    if source.read(&mut buffer[..1])? != 0 || !unchanged(&before, &rustix::fs::fstat(&source)?) {
        return Err(refused("artifact changed during copy"));
    }
    rustix::fs::fchmod(
        &target,
        Mode::from_raw_mode(if executable { 0o555 } else { 0o444 }),
    )?;
    rustix::fs::fcntl_add_seals(&target, SEALS)?;
    if !rustix::fs::fcntl_get_seals(&target)?.contains(SEALS) {
        return Err(refused("artifact seals not applied"));
    }
    let file = readonly_handle(&target)?;
    drop(target);
    budget.remaining_bytes -= bytes;
    Ok(SnapshotFile { file, bytes })
}

/// Ordinary Worker staging, distinct from immutable hidden-data publication.
/// Fixed entries only; no recursive cleanup or adoption of existing directories.
pub struct StagePublication {
    root: ArtifactRoot,
    parent: OwnedFd,
    name: String,
    dev: u64,
    ino: u64,
    files: Vec<StageEntry>,
    staged_bytes: u64,
    removed: bool,
}
const MAX_STAGE_FILES: usize = kf_contracts::judging_v2::MAX_CLASS_FILES + 1;
const MAX_STAGE_JOURNAL_BYTES: u64 = 65_536;
fn indexed_program(name: &str) -> bool {
    name.strip_prefix("program-").is_some_and(|index| {
        index.len() == 3
            && index.bytes().all(|b| b.is_ascii_digit())
            && index
                .parse::<usize>()
                .is_ok_and(|i| i < kf_contracts::judging_v2::MAX_CLASS_FILES)
    })
}
fn stage_entries_valid(names: &[&str]) -> bool {
    let indexed = names.iter().any(|n| indexed_program(n));
    let mut unique = std::collections::HashSet::new();
    names.len() <= if indexed { MAX_STAGE_FILES } else { 2 }
        && names.iter().all(|n| {
            unique.insert(*n)
                && if indexed {
                    *n == "stdin" || indexed_program(n)
                } else {
                    matches!(*n, "source" | "program" | "stdin")
                }
        })
}
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct StageEntry {
    name: String,
    dev: u64,
    ino: u64,
}
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct StageOwner {
    pid: u32,
    ticks: u64,
    boot: String,
    dev: u64,
    ino: u64,
}
fn stage_ticks(value: &str) -> io::Result<u64> {
    value
        .rsplit_once(") ")
        .and_then(|(_, s)| s.split_whitespace().nth(19))
        .and_then(|s| s.parse::<u64>().ok())
        .filter(|v| *v > 0)
        .ok_or_else(|| refused("stage owner time"))
}
impl StagePublication {
    pub fn create(parent_path: &std::path::Path, name: &str) -> io::Result<Self> {
        crate::spec::identifier(name, "stage").map_err(|_| refused("stage identifier"))?;
        let parent = rustix::fs::open(
            parent_path,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let st = rustix::fs::fstat(&parent)?;
        if st.st_uid != rustix::process::geteuid().as_raw()
            || st.st_uid < 1000
            || st.st_mode & 0o7777 != 0o700
        {
            return Err(refused("private ordinary staging parent"));
        }
        rustix::fs::mkdirat(&parent, name, Mode::from_raw_mode(0o700))?;
        let fd = rustix::fs::openat(
            &parent,
            name,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let st = rustix::fs::fstat(&fd)?;
        let value = Self {
            root: ArtifactRoot::from_directory(fd)?,
            parent,
            name: name.into(),
            dev: st.st_dev,
            ino: st.st_ino,
            files: vec![],
            staged_bytes: 0,
            removed: false,
        };
        let header = StageOwner {
            pid: std::process::id(),
            ticks: stage_ticks(&std::fs::read_to_string("/proc/self/stat")?)?,
            boot: std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?
                .trim()
                .into(),
            dev: st.st_dev,
            ino: st.st_ino,
        };
        let journal = rustix::fs::openat(
            &value.root.fd,
            ".owner",
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )?;
        let mut file = File::from(journal);
        serde_json::to_writer(&mut file, &header)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        rustix::fs::fsync(&value.root.fd)?;
        Ok(value)
    }
    /// Startup recovery authenticates both journal and inode. A reused PID or
    /// foreign object cannot confer authority; unknown content is preserved.
    pub fn recover(parent_path: &std::path::Path) -> io::Result<u32> {
        use rustix::fd::AsRawFd;
        let parent = rustix::fs::open(
            parent_path,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let st = rustix::fs::fstat(&parent)?;
        if st.st_uid != rustix::process::geteuid().as_raw()
            || st.st_uid < 1000
            || st.st_mode & 0o7777 != 0o700
        {
            return Err(refused("ordinary recovery parent"));
        }
        let names: Vec<_> = std::fs::read_dir(format!("/proc/self/fd/{}", parent.as_raw_fd()))?
            .take(513)
            .map(|e| {
                e?.file_name()
                    .into_string()
                    .map_err(|_| refused("stage name"))
            })
            .collect::<io::Result<_>>()?;
        if names.len() > 512 {
            return Err(refused("stage recovery count"));
        }
        let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
        let mut recovered = 0;
        for name in names {
            crate::spec::identifier(&name, "stage")
                .map_err(|_| refused("unrecognized staging entry retained"))?;
            let fd = rustix::fs::openat(
                &parent,
                name.as_str(),
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )?;
            let root = ArtifactRoot::from_directory(fd)?;
            let st = rustix::fs::fstat(&root.fd)?;
            let journal = rustix::fs::openat(
                &root.fd,
                ".owner",
                OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )?;
            let j = rustix::fs::fstat(&journal)?;
            if j.st_uid != root.owner
                || j.st_mode & 0o7777 != 0o600
                || j.st_nlink != 1
                || j.st_size < 0
                || j.st_size as u64 > MAX_STAGE_JOURNAL_BYTES
                || FileType::from_raw_mode(j.st_mode) != FileType::RegularFile
            {
                return Err(refused("untrusted stage journal"));
            }
            let mut bytes = String::new();
            readonly_handle(&journal)?
                .take(MAX_STAGE_JOURNAL_BYTES + 1)
                .read_to_string(&mut bytes)?;
            if bytes.len() as u64 > MAX_STAGE_JOURNAL_BYTES || !bytes.ends_with('\n') {
                return Err(refused("stage journal framing"));
            }
            let mut lines = bytes.lines();
            let owner: StageOwner =
                serde_json::from_str(lines.next().ok_or_else(|| refused("stage journal"))?)
                    .map_err(|_| refused("stage journal"))?;
            if owner.boot != boot.trim()
                || owner.pid < 2
                || owner.dev != st.st_dev
                || owner.ino != st.st_ino
            {
                return Err(refused("stage ownership binding"));
            }
            match std::fs::read_to_string(format!("/proc/{}/stat", owner.pid)) {
                Ok(stat) if stage_ticks(&stat)? == owner.ticks => continue,
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
            let entries = lines
                .map(|l| {
                    serde_json::from_str::<StageEntry>(l)
                        .map_err(|_| refused("stage entry journal"))
                })
                .collect::<io::Result<Vec<_>>>()?;
            if !stage_entries_valid(&entries.iter().map(|e| e.name.as_str()).collect::<Vec<_>>()) {
                return Err(refused("stage entry set"));
            }
            let mut value = Self {
                root,
                parent: rustix::io::fcntl_dupfd_cloexec(&parent, 3)?,
                name,
                dev: st.st_dev,
                ino: st.st_ino,
                files: entries,
                staged_bytes: 0,
                removed: false,
            };
            value.cleanup()?;
            recovered += 1;
        }
        Ok(recovered)
    }
    pub fn root(&self) -> &ArtifactRoot {
        &self.root
    }
    pub fn put(&mut self, name: &str, reader: &mut impl Read, bytes: u64) -> io::Result<()> {
        let mut names: Vec<_> = self.files.iter().map(|e| e.name.as_str()).collect();
        names.push(name);
        if !stage_entries_valid(&names)
            || bytes > MAX_SNAPSHOT_BYTES
            || (indexed_program(name) && bytes == 0)
            || self.staged_bytes.saturating_add(bytes) > MAX_SNAPSHOT_BYTES
        {
            return Err(refused("fixed staging entry/budget"));
        }
        let fd = rustix::fs::openat(
            &self.root.fd,
            name,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )?;
        let s = rustix::fs::fstat(&fd)?;
        let entry = StageEntry {
            name: name.into(),
            dev: s.st_dev,
            ino: s.st_ino,
        };
        let journal = rustix::fs::openat(
            &self.root.fd,
            ".owner",
            OFlags::WRONLY | OFlags::APPEND | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let mut file = File::from(journal);
        serde_json::to_writer(&mut file, &entry)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        self.files.push(entry);
        self.staged_bytes += bytes;
        let mut file = File::from(fd);
        let copied = io::copy(&mut reader.take(bytes + 1), &mut file)?;
        if copied != bytes {
            return Err(refused("staged artifact byte count"));
        }
        file.sync_all()
    }
    pub fn cleanup(&mut self) -> io::Result<()> {
        if self.removed {
            return Ok(());
        }
        let named = rustix::fs::statat(
            &self.parent,
            self.name.as_str(),
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        )?;
        if named.st_dev != self.dev || named.st_ino != self.ino {
            return Err(refused("foreign staging replacement"));
        }
        use rustix::fd::AsRawFd;
        let actual: Vec<_> =
            std::fs::read_dir(format!("/proc/self/fd/{}", self.root.fd.as_raw_fd()))?
                .take(MAX_STAGE_FILES + 2)
                .map(|e| Ok(e?.file_name()))
                .collect::<io::Result<_>>()?;
        if actual.len() != self.files.len() + 1
            || actual
                .iter()
                .any(|n| n != ".owner" && !self.files.iter().any(|f| n == f.name.as_str()))
        {
            return Err(refused("foreign staging entry retained"));
        }
        for entry in &self.files {
            let st = rustix::fs::statat(
                &self.root.fd,
                entry.name.as_str(),
                rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
            )?;
            if st.st_dev != entry.dev
                || st.st_ino != entry.ino
                || st.st_uid != self.root.owner
                || st.st_nlink != 1
                || FileType::from_raw_mode(st.st_mode) != FileType::RegularFile
            {
                return Err(refused("foreign staging content"));
            }
        }
        let journal = rustix::fs::statat(
            &self.root.fd,
            ".owner",
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        )?;
        if journal.st_uid != self.root.owner
            || journal.st_nlink != 1
            || journal.st_mode & 0o7777 != 0o600
            || FileType::from_raw_mode(journal.st_mode) != FileType::RegularFile
        {
            return Err(refused("foreign staging journal"));
        }
        for entry in &self.files {
            rustix::fs::unlinkat(
                &self.root.fd,
                entry.name.as_str(),
                rustix::fs::AtFlags::empty(),
            )?;
        }
        rustix::fs::unlinkat(&self.root.fd, ".owner", rustix::fs::AtFlags::empty())?;
        self.files.clear();
        rustix::fs::unlinkat(
            &self.parent,
            self.name.as_str(),
            rustix::fs::AtFlags::REMOVEDIR,
        )?;
        self.removed = true;
        Ok(())
    }
}
impl Drop for StagePublication {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

#[cfg(test)]
mod stage_policy_tests {
    use super::*;
    #[test]
    fn bounded_indexed_batch_and_legacy_names_are_separate() {
        assert!(stage_entries_valid(&["source"]));
        assert!(stage_entries_valid(&["program", "stdin"]));
        assert!(!stage_entries_valid(&["source", "program", "stdin"]));
        let mut names: Vec<_> = (0..256).map(|i| format!("program-{i:03}")).collect();
        names.push("stdin".into());
        assert!(stage_entries_valid(
            &names.iter().map(String::as_str).collect::<Vec<_>>()
        ));
        for name in [
            "program-256",
            "program-00",
            "program-0000",
            "program-../a",
            "Main.class",
            ".owner",
            "expected",
        ] {
            assert!(!stage_entries_valid(&[name]));
        }
        assert!(!stage_entries_valid(&["program-000", "source"]));
        assert!(!stage_entries_valid(&["program-000", "program"]));
        assert!(!stage_entries_valid(&["program-000", "program-000"]));
    }
}
