//! Anchored regular-file copies, filesystem error cursors and complete byte readback.

use super::{Error, Result, remaining};
use kf_contracts::restore_io::RestoreFile;
use rustix::{
    fs::{AtFlags, Mode, OFlags, ResolveFlags},
    process::{Gid, Uid},
};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs::{File, Metadata},
    io::{Read, Seek, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

const CHUNK: usize = 1024 * 1024;
const RESOLVE: ResolveFlags = ResolveFlags::BENEATH
    .union(ResolveFlags::NO_SYMLINKS)
    .union(ResolveFlags::NO_MAGICLINKS);
pub type Stamp = [u64; 9];

fn stamp(info: &Metadata) -> Result<Stamp> {
    let ns = |s: i64, n: i64| {
        u64::try_from(i128::from(s) * 1_000_000_000 + i128::from(n)).map_err(|_| Error::Identity)
    };
    Ok([
        info.dev(),
        info.ino(),
        info.mode().into(),
        info.nlink(),
        info.uid().into(),
        info.gid().into(),
        info.size(),
        ns(info.mtime(), info.mtime_nsec())?,
        ns(info.ctime(), info.ctime_nsec())?,
    ])
}

struct Anchor {
    path: PathBuf,
    file: File,
    device: u64,
    inode: u64,
}
impl Anchor {
    fn open(path: PathBuf) -> Result<Self> {
        let file = File::from(
            rustix::fs::openat2(
                rustix::fs::CWD,
                &path,
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
                Mode::empty(),
                ResolveFlags::NO_SYMLINKS | ResolveFlags::NO_MAGICLINKS,
            )
            .map_err(|_| Error::Filesystem)?,
        );
        let info = file.metadata().map_err(|_| Error::Filesystem)?;
        Ok(Self {
            path,
            file,
            device: info.dev(),
            inode: info.ino(),
        })
    }
    fn unchanged(&self) -> Result<()> {
        let fresh = Self::open(self.path.clone())?;
        if (self.device, self.inode) != (fresh.device, fresh.inode) {
            return Err(Error::Identity);
        }
        Ok(())
    }
    fn file(&self, relative: &str, flags: OFlags) -> Result<File> {
        rustix::fs::openat2(
            &self.file,
            relative,
            flags | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            if flags.contains(OFlags::CREATE) {
                Mode::from_raw_mode(0o600)
            } else {
                Mode::empty()
            },
            RESOLVE,
        )
        .map(File::from)
        .map_err(|_| Error::Filesystem)
    }
    fn observed(&self, relative: &str) -> Result<Stamp> {
        // Parents were created/checked by the locked Root operator on the
        // stopped node. The actual byte opens additionally use openat2 with
        // BENEATH/NO_SYMLINKS. Final metadata checks include the leaf's type.
        let s = rustix::fs::statat(&self.file, relative, AtFlags::SYMLINK_NOFOLLOW)
            .map_err(|_| Error::Filesystem)?;
        let ns = |sec: i64, nano: i64| {
            u64::try_from(i128::from(sec) * 1_000_000_000 + i128::from(nano))
                .map_err(|_| Error::Identity)
        };
        Ok([
            s.st_dev,
            s.st_ino,
            s.st_mode.into(),
            s.st_nlink,
            s.st_uid.into(),
            s.st_gid.into(),
            u64::try_from(s.st_size).map_err(|_| Error::Identity)?,
            ns(s.st_mtime, s.st_mtime_nsec as i64)?,
            ns(s.st_ctime, s.st_ctime_nsec as i64)?,
        ])
    }
}

pub struct Roots {
    source: Anchor,
    targets: HashMap<&'static str, Anchor>,
    source_owner: (u32, u32),
    #[cfg(test)]
    ordinary_fixture: bool,
}
impl Roots {
    pub fn production(backup_id: &str, recovery_id: &str) -> Result<Self> {
        let source = Path::new("/var/lib/kf-onlinejudge/backups")
            .join(backup_id)
            .join("objects");
        let targets = [
            (
                "postgres",
                PathBuf::from("/var/lib/kf-onlinejudge/postgres/data"),
            ),
            (
                "artifacts",
                PathBuf::from("/var/lib/kf-onlinejudge/artifacts"),
            ),
            ("imports", PathBuf::from("/var/lib/kf-onlinejudge/imports")),
            ("factory", PathBuf::from("/var/lib/kf-judge/factory")),
            ("releases", PathBuf::from("/opt/kf-onlinejudge/releases")),
            ("images", PathBuf::from("/var/lib/kf-judge/images")),
            (
                "history",
                Path::new("/var/lib/kf-onlinejudge/recovery")
                    .join(recovery_id)
                    .join("history"),
            ),
        ];
        let snapshot = Anchor::open(source.parent().ok_or(Error::Guard)?.to_owned())?;
        let roots = Self::open(source, targets)?;
        for anchor in [&snapshot, &roots.source] {
            let info = anchor.file.metadata().map_err(|_| Error::Guard)?;
            if (info.uid(), info.gid(), info.mode() & 0o7777) != (0, 0, 0o700) {
                return Err(Error::Guard);
            }
        }
        Ok(roots)
    }
    fn open<const N: usize>(
        source: PathBuf,
        targets: [(&'static str, PathBuf); N],
    ) -> Result<Self> {
        Ok(Self {
            source: Anchor::open(source)?,
            targets: targets
                .into_iter()
                .map(|(key, path)| Ok((key, Anchor::open(path)?)))
                .collect::<Result<_>>()?,
            source_owner: (0, 0),
            #[cfg(test)]
            ordinary_fixture: false,
        })
    }
    fn target<'a>(&'a self, row: &'a RestoreFile) -> Result<(&'a Anchor, &'a str, u32, u32, u32)> {
        let (domain, suffix) = row.path.split_once('/').ok_or(Error::Protocol)?;
        match self.targets.get(domain) {
            Some(anchor) => Ok((anchor, suffix, row.uid, row.gid, row.mode)),
            None => Ok((
                self.targets.get("history").ok_or(Error::Protocol)?,
                &row.path,
                0,
                0,
                0o600,
            )),
        }
    }
    pub fn sync(&self, deadline: u64) -> Result<()> {
        let mut seen = Vec::new();
        for anchor in self.targets.values() {
            remaining(deadline)?;
            anchor.unchanged()?;
            if !seen.contains(&anchor.device) {
                #[cfg(not(test))]
                rustix::fs::syncfs(&anchor.file).map_err(|_| Error::Filesystem)?;
                // Ordinary tests may fsync only their own tiny files/directories,
                // never syncfs the host. Real error-cursor validation is VM-only.
                #[cfg(test)]
                {
                    assert!(self.ordinary_fixture);
                    for entry in std::fs::read_dir(&anchor.path).map_err(|_| Error::Filesystem)? {
                        let entry = entry.map_err(|_| Error::Filesystem)?;
                        File::open(entry.path())
                            .and_then(|f| f.sync_all())
                            .map_err(|_| Error::Filesystem)?;
                    }
                    anchor.file.sync_all().map_err(|_| Error::Filesystem)?;
                }
                seen.push(anchor.device);
            }
            remaining(deadline)?;
        }
        Ok(())
    }
    pub fn unchanged(&self) -> Result<()> {
        self.source.unchanged()?;
        for anchor in self.targets.values() {
            anchor.unchanged()?;
        }
        Ok(())
    }
    pub fn copy(&self, row: RestoreFile, deadline: u64) -> Result<Pending> {
        remaining(deadline)?;
        let (anchor, name, uid, gid, mode) = self.target(&row)?;
        match rustix::fs::openat2(
            &anchor.file,
            name,
            OFlags::PATH | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
            RESOLVE,
        ) {
            Ok(fd) => {
                let info = File::from(fd).metadata().map_err(|_| Error::Filesystem)?;
                member(&info, &row, uid, gid, mode)?;
                return Ok(Pending {
                    witness: stamp(&info)?,
                    row,
                });
            }
            Err(rustix::io::Errno::NOENT) => {}
            Err(_) => return Err(Error::Filesystem),
        }
        let mut input = self
            .source
            .file(&row.sha256, OFlags::RDONLY | OFlags::NONBLOCK)?;
        let before = input.metadata().map_err(|_| Error::Filesystem)?;
        member(
            &before,
            &row,
            self.source_owner.0,
            self.source_owner.1,
            0o600,
        )?;
        let original = stamp(&before)?;
        if self.source.observed(&row.sha256)? != original {
            return Err(Error::Identity);
        }
        let mut output = anchor.file(name, OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL)?;
        let empty = output.metadata().map_err(|_| Error::Filesystem)?;
        if !empty.is_file()
            || empty.nlink() != 1
            || empty.size() != 0
            || input.stream_position().map_err(|_| Error::Filesystem)? != 0
            || output.stream_position().map_err(|_| Error::Filesystem)? != 0
        {
            return Err(Error::Identity);
        }
        transfer(&mut input, &mut output, row.bytes, deadline)?;
        if stamp(&input.metadata().map_err(|_| Error::Filesystem)?)? != original
            || self.source.observed(&row.sha256)? != original
        {
            return Err(Error::Identity);
        }
        // This descriptor was exclusively created by this copy. Its initial
        // owner/mode decides the required changes; the final fstat below still
        // checks the complete resulting file after every write and mode change.
        let changed = (empty.uid(), empty.gid()) != (uid, gid);
        if changed {
            rustix::fs::fchown(&output, Some(Uid::from_raw(uid)), Some(Gid::from_raw(gid)))
                .map_err(|_| Error::Filesystem)?;
        }
        let mode_changed = changed || empty.mode() & 0o7777 != mode;
        if mode_changed {
            rustix::fs::fchmod(&output, Mode::from_raw_mode(mode))
                .map_err(|_| Error::Filesystem)?;
        }
        let final_info = output.metadata().map_err(|_| Error::Filesystem)?;
        member(&final_info, &row, uid, gid, mode)?;
        let witness = stamp(&final_info)?;
        drop(output);
        if anchor.observed(name)? != witness {
            return Err(Error::Identity);
        }
        Ok(Pending { row, witness })
    }
    pub fn read_group(&self, group: Vec<Pending>, deadline: u64) -> Result<Vec<Pending>> {
        let mut previous = None;
        let mut reference = None;
        let mut buffer = vec![0; CHUNK];
        let mut counts = HashMap::new();
        for pending in &group {
            *counts
                .entry((&pending.row.sha256, pending.row.bytes))
                .or_insert(0) += 1;
        }
        // Keep one immutable, actually hashed <=8MiB reference per reader. Every
        // other destination is still read completely and compared byte for byte.
        let repeated: Vec<_> = group
            .iter()
            .map(|p| counts[&(&p.row.sha256, p.row.bytes)] > 1)
            .collect();
        drop(counts);
        for (pending, repeated) in group.iter().zip(repeated) {
            remaining(deadline)?;
            let row = &pending.row;
            let key = (&row.sha256, row.bytes);
            if previous != Some(key) {
                reference = None;
                previous = Some(key);
            }
            let small = repeated && row.bytes <= 8 * 1024 * 1024;
            let retain = small && reference.is_none();
            let (anchor, name, uid, gid, mode) = self.target(row)?;
            if anchor.observed(name)? != pending.witness {
                return Err(Error::Identity);
            }
            let mut input = anchor.file(name, OFlags::RDONLY | OFlags::NONBLOCK)?;
            let info = input.metadata().map_err(|_| Error::Filesystem)?;
            member(&info, row, uid, gid, mode)?;
            if stamp(&info)? != pending.witness {
                return Err(Error::Identity);
            }
            let retained = verify(
                &mut input,
                row,
                deadline,
                if small { reference.as_deref() } else { None },
                retain,
                &mut buffer,
            )?;
            // These historical destination bytes have already been synchronized
            // and fully verified. Keep their clean pages out of this finite
            // restore working set; all descriptor/path witnesses still follow.
            remaining(deadline)?;
            rustix::fs::fadvise(&input, 0, None, rustix::fs::Advice::DontNeed)
                .map_err(|_| Error::Filesystem)?;
            if stamp(&input.metadata().map_err(|_| Error::Filesystem)?)? != pending.witness
                || anchor.observed(name)? != pending.witness
            {
                return Err(Error::Identity);
            }
            if let Some(data) = retained {
                reference = Some(data);
            }
        }
        remaining(deadline)?;
        Ok(group)
    }
    pub fn final_file(&self, pending: &Pending) -> Result<()> {
        let (anchor, name, _, _, _) = self.target(&pending.row)?;
        if anchor.observed(name)? != pending.witness {
            return Err(Error::Identity);
        }
        Ok(())
    }
}

pub struct Pending {
    pub row: RestoreFile,
    pub witness: Stamp,
}

fn member(info: &Metadata, row: &RestoreFile, uid: u32, gid: u32, mode: u32) -> Result<()> {
    if !info.is_file()
        || info.nlink() != 1
        || info.size() != row.bytes
        || (info.uid(), info.gid(), info.mode() & 0o7777) != (uid, gid, mode)
    {
        return Err(Error::Identity);
    }
    Ok(())
}

fn transfer(input: &mut File, output: &mut File, bytes: u64, deadline: u64) -> Result<()> {
    let mut total = 0;
    let mut kernel = true;
    let mut buffer = Vec::new();
    while total < bytes {
        remaining(deadline)?;
        let maximum = (bytes - total).min(CHUNK as u64) as usize;
        let count = if kernel {
            match rustix::fs::sendfile(&*output, &*input, None, maximum) {
                Ok(n) => n,
                Err(rustix::io::Errno::INTR) => continue,
                Err(
                    rustix::io::Errno::NOSYS
                    | rustix::io::Errno::INVAL
                    | rustix::io::Errno::OPNOTSUPP
                    | rustix::io::Errno::XDEV,
                ) => {
                    kernel = false;
                    continue;
                }
                Err(_) => return Err(Error::Filesystem),
            }
        } else {
            if buffer.is_empty() {
                buffer.resize(CHUNK, 0);
            }
            let count = input
                .read(&mut buffer[..maximum])
                .map_err(|_| Error::Filesystem)?;
            output
                .write_all(&buffer[..count])
                .map_err(|_| Error::Filesystem)?;
            count
        };
        if count == 0 || count > maximum {
            return Err(Error::Bytes);
        }
        total += count as u64;
    }
    remaining(deadline)?;
    if input.read(&mut [0; 1]).map_err(|_| Error::Filesystem)? != 0 {
        return Err(Error::Bytes);
    }
    Ok(())
}

fn verify(
    input: &mut File,
    row: &RestoreFile,
    deadline: u64,
    reference: Option<&[u8]>,
    retain: bool,
    buffer: &mut [u8],
) -> Result<Option<Vec<u8>>> {
    if reference.is_some_and(|r| r.len() as u64 != row.bytes) {
        return Err(Error::Bytes);
    }
    let mut hasher = reference.is_none().then(Sha256::new);
    let mut retained = retain.then(|| Vec::with_capacity(row.bytes as usize));
    let mut total = 0;
    loop {
        remaining(deadline)?;
        let count = input.read(buffer).map_err(|_| Error::Filesystem)?;
        if count == 0 {
            break;
        }
        let end = total + count;
        if end as u64 > row.bytes {
            return Err(Error::Bytes);
        }
        if reference.is_some_and(|r| r[total..end] != buffer[..count]) {
            return Err(Error::Bytes);
        }
        if let Some(hash) = &mut hasher {
            hash.update(&buffer[..count]);
        }
        if let Some(data) = &mut retained {
            data.extend_from_slice(&buffer[..count]);
        }
        total = end;
        // A short read already delivered every declared byte. The caller
        // checks the same descriptor/path full nine-field witness (including
        // exact size) before and after verification, so a changed or longer
        // file still fails. Exact buffer multiples still require an explicit EOF read.
        if total as u64 == row.bytes && count < buffer.len() {
            break;
        }
    }
    if total as u64 != row.bytes
        || hasher.is_some_and(|h| format!("{:x}", h.finalize()) != row.sha256)
    {
        return Err(Error::Bytes);
    }
    remaining(deadline)?;
    Ok(retained)
}

#[cfg(test)]
pub struct Fixture(PathBuf);
#[cfg(test)]
impl Fixture {
    pub fn new() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir().join(format!(
            "kf-restore-trusted-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(root.join("source")).unwrap();
        std::fs::create_dir(root.join("target")).unwrap();
        Self(root)
    }
    pub fn source(&self) -> PathBuf {
        self.0.join("source")
    }
    pub fn target(&self) -> PathBuf {
        self.0.join("target")
    }
    pub fn roots(&self) -> Roots {
        let mut roots = Roots::open(self.source(), [("artifacts", self.target())])
            .unwrap_or_else(|_| panic!("trusted test root"));
        let info = std::fs::metadata(self.source()).unwrap();
        roots.source_owner = (info.uid(), info.gid());
        roots.ordinary_fixture = true;
        roots
    }
}
#[cfg(test)]
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
