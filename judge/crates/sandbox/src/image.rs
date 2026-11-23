//! Root-owned, immutable directory images pinned by their complete catalog.
//! All descriptor walking, ownership and byte hashing stays in Sandbox.
use kf_contracts::{
    judging::{Digest, Label},
    judging_v2::{ImageKind, RuntimeIdentity, RuntimeVersion},
};
use ring::digest;
use rustix::{
    fd::{AsRawFd, OwnedFd},
    fs::{AtFlags, FileType, Mode, OFlags, StatVfsMountFlags, StatxFlags},
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
fn catalog_path(kind: ImageKind, name: &str) -> bool {
    if kind != ImageKind::GnuCxx {
        return crate::spec::path(name, "image").is_ok();
    }
    // GNU's immutable standard-library directories include c++ and libstdc++.
    // This operator catalog rule never changes caller artifact path validation.
    !name.is_empty()
        && name.len() <= 4096
        && name.split('/').all(|p| {
            !p.is_empty()
                && p != "."
                && p != ".."
                && p.len() <= 255
                && p.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-+".contains(&b))
        })
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
            || self.architecture != "x86_64_v3"
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
            if !catalog_path(self.image_kind, name) {
                return Err(refused());
            }
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

const SQUASHFS_MAGIC: u64 = 0x7371_7368;

fn mount_id(fd: &OwnedFd) -> io::Result<u64> {
    let result = rustix::fs::statx(fd, "", AtFlags::EMPTY_PATH, StatxFlags::MNT_ID)?;
    if result.stx_mask & StatxFlags::MNT_ID.bits() == 0 {
        return Err(refused());
    }
    Ok(result.stx_mnt_id)
}

pub(crate) fn immutable_mount(fd: &OwnedFd) -> io::Result<Option<u64>> {
    if rustix::fs::fstatfs(fd)?.f_type as u64 != SQUASHFS_MAGIC
        || !rustix::fs::fstatvfs(fd)?
            .f_flag
            .contains(StatVfsMountFlags::RDONLY)
    {
        return Ok(None);
    }
    mount_id(fd).map(Some)
}

pub(crate) fn same_immutable_root(pinned: &OwnedFd, current: &OwnedFd) -> io::Result<bool> {
    let Some(mount) = immutable_mount(pinned)? else {
        return Ok(false);
    };
    let a = rustix::fs::fstat(pinned)?;
    let b = rustix::fs::fstat(current)?;
    Ok(a.st_dev == b.st_dev && a.st_ino == b.st_ino && mount_id(current)? == mount)
}

/// Every inode is checked, including unexpected entries and empty directories.
/// Cached images must all be on the same mount: the execution bind is nonrecursive.
pub(crate) fn verify(
    image: &OwnedFd,
    identity: &RuntimeIdentity,
    bytes: &[u8],
    immutable_mount: Option<u64>,
) -> io::Result<()> {
    if bytes.len() as u64 > MAX_CATALOG_BYTES || catalog_digest(bytes) != identity.image_digest {
        return Err(refused());
    }
    let catalog: ImageCatalog = serde_json::from_slice(bytes).map_err(|_| refused())?;
    catalog.validate(identity)?;
    if !compatible_cpu() {
        return Err(refused());
    }
    let mut files: BTreeMap<_, _> = catalog.files.iter().map(|f| (f.path.as_str(), f)).collect();
    let mut dirs: std::collections::BTreeSet<_> =
        catalog.directories.iter().map(String::as_str).collect();
    let root = rustix::fs::fstat(image)?;
    if root.st_uid != 0 || root.st_gid != 0 || root.st_mode & 0o7777 != 0o555 {
        return Err(refused());
    }
    let mut buffer = [0_u8; 65536];
    walk(
        image,
        "",
        (root.st_dev, immutable_mount),
        &mut files,
        &mut dirs,
        &mut buffer,
    )?;
    if !files.is_empty() || !dirs.is_empty() {
        return Err(refused());
    }
    Ok(())
}

/// Fixed images include an x86-64-v3 support-library closure. Detect the actual
/// processor/OS AVX support in Root, before the first image binary is executed.
#[cfg(target_arch = "x86_64")]
fn compatible_cpu() -> bool {
    std::is_x86_feature_detected!("sse3")
        && std::is_x86_feature_detected!("ssse3")
        && std::is_x86_feature_detected!("sse4.1")
        && std::is_x86_feature_detected!("sse4.2")
        && std::is_x86_feature_detected!("popcnt")
        && std::is_x86_feature_detected!("avx")
        && std::is_x86_feature_detected!("avx2")
        && std::is_x86_feature_detected!("bmi1")
        && std::is_x86_feature_detected!("bmi2")
        && std::is_x86_feature_detected!("fma")
        && std::is_x86_feature_detected!("f16c")
        && std::is_x86_feature_detected!("lzcnt")
        && std::is_x86_feature_detected!("cmpxchg16b")
}
#[cfg(not(target_arch = "x86_64"))]
fn compatible_cpu() -> bool {
    false
}
fn walk<'a>(
    directory: &OwnedFd,
    prefix: &str,
    filesystem: (u64, Option<u64>),
    files: &mut BTreeMap<&'a str, &'a ImageFile>,
    dirs: &mut std::collections::BTreeSet<&'a str>,
    buffer: &mut [u8],
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
        if st.st_uid != 0
            || st.st_gid != 0
            || st.st_dev != filesystem.0
            || matches!(filesystem.1, Some(mount) if mount_id(&fd)? != mount)
        {
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
                if st.st_dev != after.st_dev
                    || st.st_ino != after.st_ino
                    || matches!(filesystem.1, Some(mount) if mount_id(&child)? != mount)
                {
                    return Err(refused());
                }
                walk(&child, &path, filesystem, files, dirs, buffer)?;
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
                let mut hash = digest::Context::new(&digest::SHA256);
                let mut count = 0_u64;
                loop {
                    let n = reader.read(buffer)?;
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
                    || !matches_digest(hash.finish().as_ref(), expected.sha256.as_str())
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

fn matches_digest(digest: &[u8], expected: &str) -> bool {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let (pairs, rest) = expected.as_bytes().as_chunks::<2>();
    rest.is_empty()
        && pairs.len() == digest.len()
        && digest
            .iter()
            .zip(pairs)
            .all(|(byte, hex)| *hex == [HEX[(byte >> 4) as usize], HEX[(byte & 15) as usize]])
}

#[cfg(test)]
mod path_tests {
    use super::*;
    #[test]
    fn trusted_gnu_library_paths_do_not_widen_request_or_managed_image_paths() {
        for p in ["usr/include/c++/16.2.1/vector", "usr/lib/libstdc++.so.6"] {
            assert!(catalog_path(ImageKind::GnuCxx, p));
            assert!(!catalog_path(ImageKind::CPython313, p));
            assert!(crate::spec::path(p, "artifact").is_err());
        }
        for p in [
            "/usr/lib",
            "usr/../lib",
            "usr//lib",
            "usr/./lib",
            "usr/lib\0",
        ] {
            assert!(!catalog_path(ImageKind::GnuCxx, p));
        }
    }
}
