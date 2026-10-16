#![cfg(target_os = "linux")]
mod support;
use kf_sandbox::artifacts::ArtifactRoot;
use kf_sandbox::launcher::inputs::InputBoundTarget;
use kf_sandbox::launcher::proc_dev::RestrictedRootTarget;
use kf_sandbox::outcome::ExecutionStage;
use kf_sandbox::spec::{InputArtifact, SpecPolicy, ValidatedSpec};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
use support::{Root, binary, children};

fn spec(id: &str) -> ValidatedSpec {
    let mut request = support::spec(id, 64 << 20, 8).spec().clone();
    request.inputs.push(InputArtifact {
        artifact_id: "data-001".into(),
        path: "source/input".into(),
    });
    request.validate(&SpecPolicy::default()).unwrap()
}
struct Publications(PathBuf);
impl Publications {
    fn new() -> Self {
        support::require_vm();
        let path = PathBuf::from(format!("/kf/input-publications-{}", std::process::id()));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        for (id, value) in [
            ("program-001", b"program-payload".as_slice()),
            ("data-001", b"protected-input".as_slice()),
            ("input-001", b"stdin-private".as_slice()),
        ] {
            fs::write(path.join(id), value).unwrap();
        }
        Self(path)
    }
    fn capability(&self) -> ArtifactRoot {
        ArtifactRoot::from_directory(
            rustix::fs::open(
                &self.0,
                rustix::fs::OFlags::PATH
                    | rustix::fs::OFlags::DIRECTORY
                    | rustix::fs::OFlags::CLOEXEC,
                rustix::fs::Mode::empty(),
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
#[ignore = "requires independent capped VM and fixed read-only binding probe"]
fn inputs_are_bound_readonly_with_separate_stdin_and_protected_ancestors() {
    let root = Root::new();
    let publications = Publications::new();
    let artifacts = publications.capability();
    let before = children();
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    let one = InputBoundTarget::prepare(
        &root.validated(),
        &spec("input-bind-one"),
        &binary(),
        &artifacts,
    )
    .unwrap();
    let two = InputBoundTarget::prepare(
        &root.validated(),
        &spec("input-bind-two"),
        &binary(),
        &artifacts,
    )
    .unwrap();
    let a = format!("/proc/{}/root", one.root().init_pid());
    let b = format!("/proc/{}/root", two.root().init_pid());
    fs::write(format!("{b}/work/peer-only"), b"peer").unwrap();
    assert!(!Path::new(&format!("{a}/work/peer-only")).exists());
    assert_eq!(
        fs::read(format!("{a}/work/source/input")).unwrap(),
        b"protected-input"
    );
    fs::write(publications.0.join("data-001"), b"later source mutation").unwrap();
    assert_eq!(
        fs::write(format!("{a}/work/source/input"), b"tamper")
            .unwrap_err()
            .raw_os_error(),
        Some(30)
    );
    assert_eq!(
        fs::read(format!("{a}/work/source/input")).unwrap(),
        b"protected-input"
    );
    let mut stdin = Vec::new();
    std::io::Read::read_to_end(&mut one.stdin().unwrap().reader().unwrap(), &mut stdin).unwrap();
    assert_eq!(stdin, b"stdin-private");
    assert!(!Path::new(&format!("{a}/work/input-001")).exists());
    let output = Command::new("/kf/bin/input-bind-probe")
        .arg(one.root().init_pid().to_string())
        .env_clear()
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{:?} {:?}",
        output.stdout,
        output.stderr
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap().trim(),
        "KF_INPUT_BIND_PROBE data_ro=1 program_exec=1 data_noexec=1 ancestor_sticky=1 sibling_output=1 stdin_hidden=1 outside_absent=1"
    );
    assert_eq!(fs::read_to_string("/proc/self/mountinfo").unwrap(), mounts);
    for (target, id) in [(one, "input-bind-one"), (two, "input-bind-two")] {
        let info =
            fs::read_to_string(format!("/proc/{}/mountinfo", target.root().init_pid())).unwrap();
        assert_eq!(
            info.lines().count(),
            7,
            "five base mounts and two input files"
        );
        let error = target.refuse_remaining_isolation();
        assert!(error.cleanup_failure.is_none());
        assert!(!Path::new(&format!("/var/lib/kf-judge/tasks/kf-{id}")).exists());
    }
    assert_eq!(children(), before);
    println!(
        "KF_INPUT_BIND_PROOF immutable_bindings=1 exec_role_sealed=1 data_noexec=1 separate_stdin=1 ancestors_protected=1 sibling_outputs=1 peer_isolated=1 input_fds_closed=1 host_mounts_unchanged=1 task_cleanup=1 refusal=1"
    );
}

#[test]
#[ignore = "requires independent capped VM"]
fn input_destination_links_and_existing_files_fail_closed_and_roll_back() {
    let root = Root::new();
    let publications = Publications::new();
    let artifacts = publications.capability();
    let before = children();
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    for (id, symlink_parent) in [("input-parent-link", true), ("input-existing", false)] {
        let target =
            RestrictedRootTarget::prepare(&root.validated(), &spec(id), &binary()).unwrap();
        let work = PathBuf::from(format!("/proc/{}/root/work", target.root().init_pid()));
        if symlink_parent {
            symlink("/tmp", work.join("source")).unwrap();
        } else {
            fs::write(work.join("main"), b"must not replace").unwrap();
        }
        let Err(error) = InputBoundTarget::bind(target, &artifacts) else {
            panic!("unsafe destination accepted");
        };
        assert_eq!(error.failure.stage, ExecutionStage::RootFilesystem);
        assert_eq!(
            error.failure.errno,
            Some(if symlink_parent { 40 } else { 17 })
        );
        assert!(error.cleanup_failure.is_none());
        assert!(!root.path.join(format!("kf-{id}")).exists());
        assert!(!Path::new(&format!("/var/lib/kf-judge/tasks/kf-{id}")).exists());
    }
    assert_eq!(children(), before);
    assert_eq!(fs::read_to_string("/proc/self/mountinfo").unwrap(), mounts);
    println!(
        "KF_INPUT_BIND_FAILURE_PROOF ancestor_symlink=40 existing_file=17 partial_bindings_cleanup=1 task_cleanup=1 child_reaped=1 host_mounts_unchanged=1"
    );
}

#[test]
#[ignore = "requires independent capped VM; bounded tmpfs exhaustion"]
fn input_copy_exhaustion_refuses_and_removes_partial_bindings() {
    let root = Root::new();
    let publications = Publications::new();
    fs::write(publications.0.join("data-001"), vec![0x5a; 12_288]).unwrap();
    let mut request = spec("input-no-space").spec().clone();
    request.limits.file_bytes = 16_384;
    request.limits.workdir_bytes = 16_384;
    let request = request.validate(&SpecPolicy::default()).unwrap();
    let before = children();
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    let Err(error) = InputBoundTarget::prepare(
        &root.validated(),
        &request,
        &binary(),
        &publications.capability(),
    ) else {
        panic!("input copy exceeded bounded tmpfs");
    };
    assert_eq!(error.failure.stage, ExecutionStage::RootFilesystem);
    assert_eq!(error.failure.errno, Some(28));
    assert!(error.cleanup_failure.is_none());
    assert!(!root.path.join("kf-input-no-space").exists());
    assert!(!Path::new("/var/lib/kf-judge/tasks/kf-input-no-space").exists());
    assert_eq!(children(), before);
    assert_eq!(fs::read_to_string("/proc/self/mountinfo").unwrap(), mounts);
    println!(
        "KF_INPUT_SPACE_PROOF enospc=28 partial_bindings_cleanup=1 no_input_exemption=1 child_reaped=1 host_mounts_unchanged=1"
    );
}
