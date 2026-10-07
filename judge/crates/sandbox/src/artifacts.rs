//! File capabilities and immutable, bounded snapshots, entirely inside sandbox.
//!
//! Request values are logical IDs/relative paths. A trusted artifact provider
//! supplies an owned directory FD; no request can select a host directory.
//! Outputs are resolved only through a verified, frozen target's work FD.
//! Snapshotting is not publication, input mounting, stdio wiring or an exec permit.

use crate::spec::{ValidatedSpec, path};
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
    pub fn bytes(&self) -> u64 {
        self.bytes
    }
    #[cfg(feature = "vm-acceptance")]
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

impl ArtifactRoot {
    pub fn from_directory(fd: OwnedFd) -> io::Result<Self> {
        let stat = rustix::fs::fstat(&fd)?;
        let owner = rustix::process::geteuid().as_raw();
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
}

impl OutputPlan {
    pub(crate) fn for_spec(spec: &ValidatedSpec) -> Self {
        Self {
            paths: spec.spec().outputs.clone(),
            budget: SnapshotBudget::for_spec(spec),
        }
    }

    pub(crate) fn collect(&self, work: &OwnedFd) -> io::Result<Vec<OutputSnapshot>> {
        if FileType::from_raw_mode(rustix::fs::fstat(work)?.st_mode) != FileType::Directory
            || self.paths.len() > 16
        {
            return Err(refused("invalid output directory or count"));
        }
        let mut budget = self.budget;
        let mut files = Vec::with_capacity(self.paths.len());
        for path in &self.paths {
            files.push(OutputSnapshot {
                path: path.clone(),
                file: snapshot(work, path, &mut budget, None, false)?,
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
    path(relative, "artifact").map_err(|_| refused("noncanonical artifact path"))?;
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
