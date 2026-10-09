#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
//! Indexed Worker publications, actual seals/recovery/foreign preservation in VM only.
#[allow(dead_code)]
mod support;
use kf_sandbox::artifacts::StagePublication;
use std::{
    fs,
    io::Read,
    os::unix::{
        fs::{MetadataExt, PermissionsExt, chown},
        process::CommandExt,
    },
    path::PathBuf,
    process::Command,
};

fn child(name: &str, path: &std::path::Path) -> std::process::Output {
    Command::new(std::env::current_exe().unwrap())
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .env("KF_INDEXED_STAGE_DIR", path)
        .uid(1000)
        .gid(1000)
        .args([
            "--ignored",
            "--exact",
            name,
            "--test-threads=1",
            "--nocapture",
        ])
        .output()
        .unwrap()
}

#[test]
#[ignore = "ordinary peer subprocess in capped independent VM"]
fn leave_indexed_publication_for_recovery() {
    support::require_vm();
    assert_eq!(rustix::process::geteuid().as_raw(), 1000);
    let path = PathBuf::from(std::env::var_os("KF_INDEXED_STAGE_DIR").unwrap());
    let mut p = StagePublication::create(&path, "dead-owner").unwrap();
    for i in 0..256 {
        p.put(&format!("program-{i:03}"), &mut b"class".as_slice(), 5)
            .unwrap();
    }
    p.put("stdin", &mut b"".as_slice(), 0).unwrap();
    assert!(p.put("program-256", &mut b"class".as_slice(), 5).is_err());
    assert!(p.put("program", &mut b"class".as_slice(), 5).is_err());
    assert!(p.put("program-001", &mut b"class".as_slice(), 5).is_err());
    assert!(fs::metadata(path.join("dead-owner/.owner")).unwrap().len() < 65536);
    std::mem::forget(p);
    println!("KF_INDEXED_DEAD_OWNER=1");
}

#[test]
#[ignore = "ordinary peer subprocess in capped independent VM"]
fn ordinary_indexed_staging_client() {
    support::require_vm();
    assert_eq!(rustix::process::geteuid().as_raw(), 1000);
    let path = PathBuf::from(std::env::var_os("KF_INDEXED_STAGE_DIR").unwrap());
    let mut p = StagePublication::create(&path, "sealed").unwrap();
    p.put("program-000", &mut b"\0checked\xff".as_slice(), 9)
        .unwrap();
    assert!(p.put("program-001", &mut b"".as_slice(), 0).is_err());
    for name in [
        "../foreign",
        "program-1",
        "program-256",
        "source",
        "program",
    ] {
        assert!(p.put(name, &mut b"x".as_slice(), 1).is_err());
    }
    assert!(
        p.put("program-001", &mut b"x".as_slice(), 64 * 1024 * 1024 + 1)
            .is_err()
    );
    let snapshot = p.root().snapshot_artifact("program-000", 9).unwrap();
    fs::write(
        path.join("sealed/program-000"),
        b"overwritten backing bytes",
    )
    .unwrap();
    let mut bytes = vec![];
    snapshot.reader().unwrap().read_to_end(&mut bytes).unwrap();
    assert_eq!(bytes, b"\0checked\xff");
    let seals = rustix::fs::fcntl_get_seals(&snapshot).unwrap();
    assert!(seals.contains(
        rustix::fs::SealFlags::WRITE
            | rustix::fs::SealFlags::GROW
            | rustix::fs::SealFlags::SHRINK
            | rustix::fs::SealFlags::SEAL
    ));
    assert_eq!(StagePublication::recover(&path).unwrap(), 0);
    p.cleanup().unwrap();
    let owner = child("leave_indexed_publication_for_recovery", &path);
    assert!(owner.status.success(), "{owner:?}");
    assert!(String::from_utf8_lossy(&owner.stdout).contains("KF_INDEXED_DEAD_OWNER=1"));
    let foreign = path.join("dead-owner/foreign");
    fs::write(&foreign, b"foreign canary").unwrap();
    let inode = fs::metadata(&foreign).unwrap().ino();
    assert!(StagePublication::recover(&path).is_err());
    assert_eq!(fs::metadata(&foreign).unwrap().ino(), inode);
    assert_eq!(fs::read(&foreign).unwrap(), b"foreign canary");
    fs::remove_file(foreign).unwrap();
    assert_eq!(StagePublication::recover(&path).unwrap(), 1);
    assert_eq!(fs::read_dir(&path).unwrap().count(), 0);
    let mut p = StagePublication::create(&path, "replaced").unwrap();
    p.put("program-000", &mut b"original".as_slice(), 8)
        .unwrap();
    let file = path.join("replaced/program-000");
    fs::rename(&file, path.join("original-retained")).unwrap();
    fs::write(&file, b"different inode").unwrap();
    let inode = fs::metadata(&file).unwrap().ino();
    assert!(p.cleanup().is_err());
    assert_eq!(fs::metadata(&file).unwrap().ino(), inode);
    assert_eq!(fs::read(&file).unwrap(), b"different inode");
    fs::remove_file(&file).unwrap();
    fs::rename(path.join("original-retained"), &file).unwrap();
    p.cleanup().unwrap();
    assert_eq!(fs::read_dir(&path).unwrap().count(), 0);
    println!(
        "KF_INDEXED_STAGING_PROOF files=256 stdin=1 bounds=1 seals=1 live_owner=1 dead_owner_recovered=1 foreign_inode_preserved=1 replaced_inode_preserved=1 leftovers=0"
    );
}

#[test]
#[ignore = "actual ordinary indexed staging only inside independent capped no-NIC/no-share VM"]
fn indexed_staging_recovers_only_owned_inodes_and_preserves_sealed_bytes() {
    support::require_vm();
    let dir = PathBuf::from(format!("/kf/indexed-staging-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    chown(&dir, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    let output = child("ordinary_indexed_staging_client", &dir);
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains("KF_INDEXED_STAGING_PROOF"));
    assert_eq!(fs::read_dir(&dir).unwrap().count(), 0);
    fs::remove_dir(dir).unwrap();
    print!("{}", String::from_utf8_lossy(&output.stdout));
    println!("KF_INDEXED_STAGING_STATUS=0");
}
