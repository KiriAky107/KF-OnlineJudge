//! Root-owned, immutable directory images pinned by their complete catalog.
//! All descriptor walking, ownership and byte hashing stays in Sandbox.
use kf_contracts::{
    judging::{Digest, Label},
    judging_v2::{ImageKind, RuntimeIdentity, RuntimeVersion},
};
use rustix::{
    fd::{AsRawFd, OwnedFd},
    fs::{FileType, Mode, OFlags},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{self, Read},
};

pub const MAX_CATALOG_BYTES: u64 = 1024 * 1024;
const MAX_FILES: usize = 4096;
const MAX_IMAGE_BYTES: u64 = 1024 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImageFile {
    pub path: String,
    pub bytes: u64,
    pub sha256: Digest,
    pub executable: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImageCatalog {
    pub format_version: u16,
    pub architecture: String,
    pub image_kind: ImageKind,
    pub image_revision: u32,
    pub toolchain_id: Label,
    pub runtime_version: RuntimeVersion,
    pub directories: Vec<String>,
    pub files: Vec<ImageFile>,
}
fn refused() -> io::Error {
    io::Error::new(
        io::ErrorKind::PermissionDenied,
        "image catalog/integrity refused",
    )
}
pub fn catalog_digest(data: &[u8]) -> Digest {
    format!("{:x}", Sha256::digest(data))
        .try_into()
        .expect("SHA256")
}
impl ImageCatalog {
    pub fn validate(&self, identity: &RuntimeIdentity) -> io::Result<()> {
        let language = match self.image_kind {
            ImageKind::GnuCxx => kf_contracts::judging_v2::Language::C17,
            ImageKind::CPython313 => kf_contracts::judging_v2::Language::Python313,
            ImageKind::OpenJdk21 => kf_contracts::judging_v2::Language::Java21,
        };
        identity
            .validate_for_language(language)
            .map_err(|_| refused())?;
        if self.format_version != 2
            || self.architecture != "x86_64"
            || self.image_kind != identity.image_kind
            || self.image_revision != identity.image_revision
            || self.toolchain_id != identity.toolchain_id
            || self.runtime_version != identity.runtime_version
            || self.files.is_empty()
            || self.files.len() > MAX_FILES
            || self.directories.len() > MAX_FILES
            || self.directories.windows(2).any(|v| v[0] >= v[1])
            || self.files.windows(2).any(|v| v[0].path >= v[1].path)
        {
            return Err(refused());
        }
        let mut total = 0_u64;
        for name in self
            .directories
            .iter()
            .chain(self.files.iter().map(|v| &v.path))
        {
            crate::spec::path(name, "image").map_err(|_| refused())?;
            if name == "manifest.json"
                || name
                    .split('/')
                    .any(|v| matches!(v, "site-packages" | "__pycache__"))
                || name.ends_with(".pth")
                || name.ends_with("/sitecustomize.py")
                || name.ends_with("/usercustomize.py")
            {
                return Err(refused());
            }
        }
        for file in &self.files {
            total = total
                .checked_add(file.bytes)
                .filter(|v| *v <= MAX_IMAGE_BYTES)
                .ok_or_else(refused)?;
        }
        if self
            .files
            .iter()
            .any(|f| self.directories.contains(&f.path))
            || ["dev", "proc", "tmp", "work"]
                .iter()
                .any(|p| !self.directories.iter().any(|d| d == p))
        {
            return Err(refused());
        }
        let required: &[&str] = match self.image_kind {
            ImageKind::CPython313 => &[
                "usr/bin/python3.13",
                "usr/lib/kf/python_check.py",
                "usr/lib/python3.13/os.py",
                "lib64/ld-linux-x86-64.so.2",
            ],
            ImageKind::OpenJdk21 => &[
                "usr/lib/jvm/bin/java",
                "usr/lib/jvm/bin/javac",
                "usr/lib/jvm/lib/modules",
                "usr/lib/jvm/lib/ct.sym",
                "lib64/ld-linux-x86-64.so.2",
            ],
            ImageKind::GnuCxx => &["usr/bin/gcc", "usr/bin/g++", "lib64/ld-linux-x86-64.so.2"],
        };
        if required
            .iter()
            .any(|p| !self.files.iter().any(|f| f.path == *p && f.bytes != 0))
        {
            return Err(refused());
        }
        Ok(())
    }
}

/// Every inode is checked, including unexpected entries and empty directories.
/// No symlinks, hard links, writable files, special files or mount crossings.
pub(crate) fn verify(image: &OwnedFd, identity: &RuntimeIdentity, bytes: &[u8]) -> io::Result<()> {
    if bytes.len() as u64 > MAX_CATALOG_BYTES || catalog_digest(bytes) != identity.image_digest {
        return Err(refused());
    }
    let catalog: ImageCatalog = serde_json::from_slice(bytes).map_err(|_| refused())?;
    catalog.validate(identity)?;
    let mut files: BTreeMap<_, _> = catalog.files.iter().map(|f| (f.path.as_str(), f)).collect();
    let mut dirs: std::collections::BTreeSet<_> =
        catalog.directories.iter().map(String::as_str).collect();
    let root = rustix::fs::fstat(image)?;
    if root.st_uid != 0 || root.st_gid != 0 || root.st_mode & 0o7777 != 0o555 {
        return Err(refused());
    }
    walk(image, "", root.st_dev, &mut files, &mut dirs)?;
    if !files.is_empty() || !dirs.is_empty() {
        return Err(refused());
    }
    Ok(())
}
fn walk<'a>(
    directory: &OwnedFd,
    prefix: &str,
    device: u64,
    files: &mut BTreeMap<&'a str, &'a ImageFile>,
    dirs: &mut std::collections::BTreeSet<&'a str>,
) -> io::Result<()> {
    if prefix.split('/').count() > 32 {
        return Err(refused());
    }
    for entry in std::fs::read_dir(format!("/proc/self/fd/{}", directory.as_raw_fd()))? {
        let name = entry?.file_name();
        let name = name.to_str().ok_or_else(refused)?;
        let path = if prefix.is_empty() {
            name.into()
        } else {
            format!("{prefix}/{name}")
        };
        let fd = rustix::fs::openat(
            directory,
            name,
            OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let st = rustix::fs::fstat(&fd)?;
        if st.st_uid != 0 || st.st_gid != 0 || st.st_dev != device {
            return Err(refused());
        }
        match FileType::from_raw_mode(st.st_mode) {
            FileType::Directory => {
                if st.st_mode & 0o7777 != 0o555 || !dirs.remove(path.as_str()) {
                    return Err(refused());
                }
                let child = rustix::fs::openat(
                    directory,
                    name,
                    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                    Mode::empty(),
                )?;
                let after = rustix::fs::fstat(&child)?;
                if st.st_dev != after.st_dev || st.st_ino != after.st_ino {
                    return Err(refused());
                }
                walk(&child, &path, device, files, dirs)?;
            }
            FileType::RegularFile => {
                if st.st_nlink != 1 || st.st_size < 0 {
                    return Err(refused());
                }
                if path == "manifest.json" {
                    if st.st_mode & 0o7777 != 0o444 {
                        return Err(refused());
                    }
                    continue;
                }
                let expected = files.remove(path.as_str()).ok_or_else(refused)?;
                if st.st_size as u64 != expected.bytes
                    || st.st_mode & 0o7777 != if expected.executable { 0o555 } else { 0o444 }
                {
                    return Err(refused());
                }
                let read = rustix::fs::open(
                    format!("/proc/self/fd/{}", fd.as_raw_fd()),
                    OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NONBLOCK | OFlags::NOCTTY,
                    Mode::empty(),
                )?;
                let after = rustix::fs::fstat(&read)?;
                if st.st_dev != after.st_dev || st.st_ino != after.st_ino {
                    return Err(refused());
                }
                let mut reader = File::from(read);
                let mut hash = Sha256::new();
                let mut count = 0_u64;
                let mut buffer = [0_u8; 65536];
                loop {
                    let n = reader.read(&mut buffer)?;
                    if n == 0 {
                        break;
                    }
                    count += n as u64;
                    if count > expected.bytes {
                        return Err(refused());
                    }
                    hash.update(&buffer[..n]);
                }
                if count != expected.bytes
                    || format!("{:x}", hash.finalize()) != expected.sha256.as_str()
                {
                    return Err(refused());
                }
                let final_stat = reader.metadata()?;
                use std::os::unix::fs::MetadataExt;
                if final_stat.dev() != st.st_dev
                    || final_stat.ino() != st.st_ino
                    || final_stat.nlink() != 1
                    || final_stat.len() != expected.bytes
                    || final_stat.mode() & 0o7777 != st.st_mode & 0o7777
                    || final_stat.uid() != 0
                    || final_stat.gid() != 0
                {
                    return Err(refused());
                }
            }
            _ => return Err(refused()),
        }
    }
    Ok(())
}
