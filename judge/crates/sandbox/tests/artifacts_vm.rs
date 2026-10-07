#![cfg(target_os = "linux")]
//! Path substitution, special-file and mutation tests run only in the capped VM.
mod support;
use kf_sandbox::artifacts::{ArtifactRoot, SnapshotFile};
use kf_sandbox::launcher::proc_dev::RestrictedRootTarget;
use kf_sandbox::spec::{ExecutionSpec, SpecPolicy, ValidatedSpec};
use rustix::fd::{AsFd, AsRawFd};
use rustix::fs::{FileType, Mode, OFlags};
use std::fs;
use std::io::Read;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use support::{Root, binary, children, require_vm};

fn request(id: &str, outputs: &[&str]) -> ExecutionSpec {
    let mut s = support::spec(id, 64 << 20, 8).spec().clone();
    s.outputs = outputs.iter().map(|s| (*s).into()).collect();
    s
}
fn validated(s: ExecutionSpec) -> ValidatedSpec {
    s.validate(&SpecPolicy::default()).unwrap()
}
fn read(file: &SnapshotFile) -> Vec<u8> {
    let mut bytes = Vec::new();
    file.reader().unwrap().read_to_end(&mut bytes).unwrap();
    bytes
}
fn descriptors() -> usize {
    fs::read_dir("/proc/self/fd").unwrap().count()
}
struct Publications(PathBuf);
impl Publications {
    fn new(id: &str) -> Self {
        require_vm();
        let path = PathBuf::from(format!("/kf/artifacts-{id}"));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self(path)
    }
    fn capability(&self) -> ArtifactRoot {
        ArtifactRoot::from_directory(
            rustix::fs::open(
                &self.0,
                OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .unwrap(),
        )
        .unwrap()
    }
}
impl Drop for Publications {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn input_and_stdin_snapshots_are_sealed_and_do_not_follow_replacement() {
    let publications = Publications::new("immutable");
    fs::write(publications.0.join("program-001"), b"\0binary\xff").unwrap();
    fs::write(publications.0.join("input-001"), []).unwrap();
    let capability = publications.capability();
    let before = descriptors();
    let prepared = capability
        .snapshot_inputs(&validated(request("input-seals", &[])))
        .unwrap();
    assert_eq!(prepared.files.len(), 1);
    assert_eq!(prepared.files[0].path, "main");
    assert!(prepared.files[0].executable);
    assert_eq!(read(&prepared.files[0].file), b"\0binary\xff");
    assert_eq!(prepared.stdin.as_ref().unwrap().bytes(), 0);
    assert!(read(prepared.stdin.as_ref().unwrap()).is_empty());
    fs::rename(
        publications.0.join("program-001"),
        publications.0.join("old"),
    )
    .unwrap();
    fs::write(publications.0.join("program-001"), b"replacement").unwrap();
    fs::write(publications.0.join("old"), b"overwritten backing source").unwrap();
    assert_eq!(read(&prepared.files[0].file), b"\0binary\xff");
    let seals = rustix::fs::fcntl_get_seals(&prepared.files[0].file).unwrap();
    for required in [
        rustix::fs::SealFlags::WRITE,
        rustix::fs::SealFlags::GROW,
        rustix::fs::SealFlags::SHRINK,
        rustix::fs::SealFlags::SEAL,
    ] {
        assert!(seals.contains(required));
    }
    assert!(
        rustix::io::fcntl_getfd(&prepared.files[0].file)
            .unwrap()
            .contains(rustix::io::FdFlags::CLOEXEC)
    );
    // Even a privileged test that reopens the retained inode writable cannot
    // modify/shrink/grow it. This is a real seal, beyond descriptor permissions.
    let fd = rustix::fs::open(
        format!(
            "/proc/self/fd/{}",
            prepared.files[0].file.as_fd().as_raw_fd()
        ),
        OFlags::RDWR | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .unwrap();
    assert_eq!(
        rustix::io::pwrite(&fd, b"tamper", 0),
        Err(rustix::io::Errno::PERM)
    );
    assert_eq!(rustix::fs::ftruncate(&fd, 0), Err(rustix::io::Errno::PERM));
    assert_eq!(
        rustix::fs::ftruncate(&fd, 100),
        Err(rustix::io::Errno::PERM)
    );
    assert_eq!(read(&prepared.files[0].file), b"\0binary\xff");
    drop(fd);
    drop(prepared);
    assert_eq!(descriptors(), before);
    println!(
        "KF_ARTIFACT_INPUT_PROOF binary=1 empty_stdin_separate=1 source_replacement_safe=1 kernel_seals=1 readonly_cloexec=1 no_fd_leaks=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn input_publications_reject_links_special_files_modes_and_byte_limits() {
    let publications = Publications::new("refusals");
    let capability = publications.capability();
    let mut s = request("input-refusals", &[]);
    s.stdin_artifact = None;
    s.limits.file_bytes = 4;
    let spec = validated(s);
    let candidate = publications.0.join("program-001");
    let before = descriptors();
    for variant in 0..10 {
        match variant {
            0 => symlink("/image-outside-sentinel", &candidate).unwrap(),
            1 => {
                fs::write(publications.0.join("other"), b"ok").unwrap();
                fs::hard_link(publications.0.join("other"), &candidate).unwrap();
            }
            2 => rustix::fs::mknodat(
                rustix::fs::CWD,
                &candidate,
                FileType::Fifo,
                Mode::from_raw_mode(0o600),
                0,
            )
            .unwrap(),
            3 => rustix::fs::mknodat(
                rustix::fs::CWD,
                &candidate,
                FileType::CharacterDevice,
                Mode::from_raw_mode(0o600),
                rustix::fs::makedev(1, 3),
            )
            .unwrap(),
            4 => fs::create_dir(&candidate).unwrap(),
            5 => {
                fs::write(&candidate, b"ok").unwrap();
                fs::set_permissions(&candidate, fs::Permissions::from_mode(0o666)).unwrap();
            }
            6 => {
                fs::write(&candidate, b"ok").unwrap();
                rustix::fs::chown(
                    &candidate,
                    Some(rustix::process::Uid::from_raw(100000)),
                    None,
                )
                .unwrap();
            }
            7 => fs::write(&candidate, b"large").unwrap(),
            8 => {
                let _listener = std::os::unix::net::UnixListener::bind(&candidate).unwrap();
            }
            9 => {
                fs::write(&candidate, b"ok").unwrap();
                fs::set_permissions(&candidate, fs::Permissions::from_mode(0o4755)).unwrap();
            }
            _ => unreachable!(),
        }
        assert!(
            capability.snapshot_inputs(&spec).is_err(),
            "variant {variant}"
        );
        if variant == 4 {
            fs::remove_dir(&candidate).unwrap();
        } else {
            fs::remove_file(&candidate).unwrap();
        }
        assert_eq!(descriptors(), before);
    }
    fs::set_permissions(&publications.0, fs::Permissions::from_mode(0o777)).unwrap();
    assert!(capability.snapshot_inputs(&spec).is_err());
    let directory = rustix::fs::open(
        &publications.0,
        OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .unwrap();
    assert!(ArtifactRoot::from_directory(directory).is_err());
    fs::set_permissions(&publications.0, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&candidate, [0_u8; 2048]).unwrap();
    let mut s = request("input-total", &[]);
    s.stdin_artifact = None;
    s.limits.file_bytes = 2048;
    s.limits.workdir_bytes = 8192;
    for n in 0..4 {
        s.inputs.push(kf_sandbox::spec::InputArtifact {
            artifact_id: "program-001".into(),
            path: format!("copy{n}"),
        });
    }
    assert!(capability.snapshot_inputs(&validated(s)).is_err());
    assert_eq!(descriptors(), before);
    println!(
        "KF_ARTIFACT_REFUSAL_PROOF symlink=1 hardlink=1 fifo=1 device=1 socket=1 setuid=1 directory=1 mode=1 owner=1 oversize=1 total_limit=1 partial_fd_cleanup=1 untrusted_root=1 changed_root_authority=1 no_fd_leaks=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn declared_outputs_use_the_frozen_work_root_and_enforce_file_and_batch_limits() {
    let root = Root::new();
    let before = children();
    let mut s = request("artifact-export", &["nested/binary", "empty"]);
    s.limits.file_bytes = 32 << 10;
    s.limits.workdir_bytes = 64 << 10;
    let target =
        RestrictedRootTarget::prepare(&root.validated(), &validated(s), &binary()).unwrap();
    let work = PathBuf::from(format!("/proc/{}/root/work", target.root().init_pid()));
    fs::create_dir(work.join("nested")).unwrap();
    fs::write(work.join("nested/binary"), b"\0output\xff").unwrap();
    fs::write(work.join("empty"), []).unwrap();
    fs::write(work.join("undeclared"), b"not exported").unwrap();
    let fds = descriptors();
    let outputs = target.root().snapshot_outputs().unwrap();
    assert_eq!(outputs.len(), 2);
    assert_eq!(outputs[0].path, "nested/binary");
    assert_eq!(read(&outputs[0].file), b"\0output\xff");
    assert_eq!(outputs[1].file.bytes(), 0);
    fs::write(work.join("nested/binary"), b"subsequent change").unwrap();
    assert_eq!(read(&outputs[0].file), b"\0output\xff");
    drop(outputs);
    assert_eq!(descriptors(), fds);
    fs::File::create(work.join("nested/binary"))
        .unwrap()
        .set_len(32769)
        .unwrap();
    assert!(target.root().snapshot_outputs().is_err());
    assert_eq!(descriptors(), fds);
    fs::File::create(work.join("nested/binary"))
        .unwrap()
        .set_len(32768)
        .unwrap();
    fs::File::create(work.join("empty"))
        .unwrap()
        .set_len(32768)
        .unwrap();
    let outputs = target.root().snapshot_outputs().unwrap();
    assert_eq!(outputs.iter().map(|o| o.file.bytes()).sum::<u64>(), 65536);
    drop(outputs);
    assert!(
        target
            .refuse_remaining_isolation()
            .cleanup_failure
            .is_none()
    );
    assert_eq!(children(), before);
    assert!(!Path::new("/var/lib/kf-judge/tasks/kf-artifact-export").exists());

    let mut s = request("artifact-total", &["one", "two", "three"]);
    s.limits.file_bytes = 32768;
    s.limits.workdir_bytes = 65536;
    let target =
        RestrictedRootTarget::prepare(&root.validated(), &validated(s), &binary()).unwrap();
    let work = PathBuf::from(format!("/proc/{}/root/work", target.root().init_pid()));
    for name in ["one", "two", "three"] {
        fs::File::create(work.join(name))
            .unwrap()
            .set_len(32768)
            .unwrap();
    }
    let fds = descriptors();
    assert!(
        target.root().snapshot_outputs().is_err(),
        "aggregate sparse-file logical bytes"
    );
    assert_eq!(descriptors(), fds);
    assert!(
        target
            .refuse_remaining_isolation()
            .cleanup_failure
            .is_none()
    );
    assert_eq!(children(), before);
    println!(
        "KF_ARTIFACT_EXPORT_PROOF binary=1 empty=1 declared_only=1 private_work=1 immutable_snapshot=1 file_limit=1 total_limit=1 sparse_bytes_counted=1 partial_fd_cleanup=1 task_cleanup=1"
    );
}

#[test]
#[ignore = "requires independent VM with externally verified whole-VM caps"]
fn output_resolution_rejects_links_special_files_submounts_and_path_swaps() {
    let root = Root::new();
    let target = RestrictedRootTarget::prepare(
        &root.validated(),
        &validated(request("artifact-paths", &["nested/value"])),
        &binary(),
    )
    .unwrap();
    let work = PathBuf::from(format!("/proc/{}/root/work", target.root().init_pid()));
    fs::create_dir(work.join("nested")).unwrap();
    let candidate = work.join("nested/value");
    let before = descriptors();
    for variant in 0..6 {
        match variant {
            0 => symlink("/image-outside-sentinel", &candidate).unwrap(),
            1 => {
                fs::write(work.join("other"), b"hardlinked").unwrap();
                fs::hard_link(work.join("other"), &candidate).unwrap();
            }
            2 => rustix::fs::mknodat(
                rustix::fs::CWD,
                &candidate,
                FileType::Fifo,
                Mode::from_raw_mode(0o600),
                0,
            )
            .unwrap(),
            3 => rustix::fs::mknodat(
                rustix::fs::CWD,
                &candidate,
                FileType::CharacterDevice,
                Mode::from_raw_mode(0o600),
                rustix::fs::makedev(1, 3),
            )
            .unwrap(),
            4 => fs::create_dir(&candidate).unwrap(),
            5 => symlink("/proc/self/fd/0", &candidate).unwrap(),
            _ => unreachable!(),
        }
        assert!(
            target.root().snapshot_outputs().is_err(),
            "variant {variant}"
        );
        if variant == 4 {
            fs::remove_dir(&candidate).unwrap();
        } else {
            fs::remove_file(&candidate).unwrap();
        }
        assert_eq!(descriptors(), before);
    }
    fs::remove_dir(work.join("nested")).unwrap();
    symlink("/kf", work.join("nested")).unwrap();
    assert!(target.root().snapshot_outputs().is_err());
    fs::remove_file(work.join("nested")).unwrap();
    fs::create_dir(work.join("nested")).unwrap();
    fs::write(&candidate, b"inside-only").unwrap();
    // A private mount placed by the trusted VM test is rejected even when its
    // file has the same inode type. User mount permissions remain unimplemented.
    let probe = std::process::Command::new("/kf/bin/artifact-mount-probe")
        .arg(target.root().init_pid().to_string())
        .output()
        .unwrap();
    assert!(probe.status.success(), "{:?}", probe.stderr);
    assert!(target.root().snapshot_outputs().is_err());
    let probe = std::process::Command::new("/kf/bin/artifact-mount-probe")
        .arg(target.root().init_pid().to_string())
        .arg("undo")
        .output()
        .unwrap();
    assert!(probe.status.success());
    let stop = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&stop);
    let directory = work.clone();
    let attacker = std::thread::spawn(move || {
        while !flag.load(Ordering::Relaxed) {
            fs::rename(directory.join("nested"), directory.join("held")).unwrap();
            symlink("/kf", directory.join("nested")).unwrap();
            fs::remove_file(directory.join("nested")).unwrap();
            fs::rename(directory.join("held"), directory.join("nested")).unwrap();
        }
    });
    let mut accepted = 0;
    let mut rejected = 0;
    for _ in 0..200 {
        match target.root().snapshot_outputs() {
            Ok(outputs) => {
                assert_eq!(read(&outputs[0].file), b"inside-only");
                accepted += 1;
            }
            Err(_) => rejected += 1,
        }
    }
    stop.store(true, Ordering::Relaxed);
    attacker.join().unwrap();
    assert_eq!(accepted + rejected, 200);
    assert_eq!(descriptors(), before);
    assert!(
        target
            .refuse_remaining_isolation()
            .cleanup_failure
            .is_none()
    );
    for invalid in [
        "../outside",
        "/outside",
        "nested/../outside",
        "nested//value",
        "nested/./value",
    ] {
        assert!(
            request("artifact-lexical", &[invalid])
                .validate(&SpecPolicy::default())
                .is_err()
        );
    }
    let too_many: Vec<String> = (0..17).map(|n| format!("file{n}")).collect();
    let mut s = request("artifact-count", &[]);
    s.outputs = too_many;
    assert!(s.validate(&SpecPolicy::default()).is_err());
    println!(
        "KF_ARTIFACT_PATH_PROOF symlink=1 hardlink=1 fifo=1 device=1 directory=1 magiclink=1 ancestor_symlink=1 submount=1 traversal=1 count=1 rename_race_attempts=200 accepted={accepted} rejected={rejected} outside_content_absent=1 no_fd_leaks=1"
    );
}
