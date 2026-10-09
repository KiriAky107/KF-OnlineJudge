#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
//! One default Root factory, one global pool, and an ordinary UID client.
#[allow(dead_code)]
mod support;
use kf_contracts::judging_v2::RuntimeIdentity;
use kf_sandbox::{
    artifacts::ArtifactRoot,
    factory::FactoryClient,
    image::{ImageCatalog, catalog_digest},
    outcome::{CleanupState, ExecutionOutcome},
    profiles,
    runtime::{ImageKind, RegisteredImage},
    spec::*,
};
use std::{
    fs,
    io::Read,
    os::unix::{
        fs::{MetadataExt, PermissionsExt, chown},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

fn store(path: &Path, bytes: &[u8]) {
    fs::write(path, bytes).unwrap();
    chown(path, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}
fn spec(image: &RegisteredImage, id: &str, profile: ExecutionProfile) -> ExecutionSpec {
    let mut limits = ResourceLimits::for_profile(profile);
    limits.cpu_time_us = 30_000_000;
    limits.wall_time_us = 60_000_000;
    ExecutionSpec {
        schema_version: kf_contracts::SchemaVersion,
        runtime_identity: Some(image.runtime_identity.clone()),
        run_id: id.into(),
        profile,
        argv: profiles::argv(profile, &limits).unwrap(),
        env: Default::default(),
        cwd: ".".into(),
        inputs: vec![InputArtifact {
            artifact_id: "source".into(),
            path: if profile == ExecutionProfile::JavaCompile {
                "Main.java"
            } else {
                "main.py"
            }
            .into(),
        }],
        stdin_artifact: None,
        outputs: vec![],
        limits,
    }
}
fn bytes(file: &kf_sandbox::artifacts::SnapshotFile) -> Vec<u8> {
    assert!(file.bytes() <= 1 << 20);
    let mut data = Vec::new();
    file.reader()
        .unwrap()
        .take(file.bytes() + 1)
        .read_to_end(&mut data)
        .unwrap();
    assert_eq!(data.len() as u64, file.bytes());
    data
}
fn run(
    client: &FactoryClient,
    root: &ArtifactRoot,
    spec: ExecutionSpec,
    cancel: bool,
) -> kf_sandbox::rpc::Executed {
    let (result, pid) = client
        .execute_controlled(
            spec.clone(),
            root,
            Some(Instant::now() + Duration::from_secs(if cancel { 15 } else { 180 })),
            None,
        )
        .unwrap();
    let validated = spec.clone().validate(&SpecPolicy::default()).unwrap();
    assert_eq!(result.report.cleanup, CleanupState::Complete);
    assert!(result.report.errors.is_empty());
    if cancel {
        assert!(matches!(
            result.report.classify(&validated).unwrap(),
            ExecutionOutcome::Cancelled
        ));
        assert!(bytes(&result.stdout).starts_with(b"live-"));
    } else {
        assert!(matches!(
            result.report.classify(&validated).unwrap(),
            ExecutionOutcome::Exited { code: 0 }
        ));
    }
    println!(
        "KF_MANAGED_FACTORY_STAGE {}",
        serde_json::json!({"ordinary_uid":1000,"helper_pid":pid,"spec":spec,"report":result.report})
    );
    result
}
#[test]
#[ignore = "invoked only as bounded ordinary child by the independent VM fixture"]
fn ordinary_registry_client() {
    assert_eq!(rustix::process::geteuid().as_raw(), 1000);
    assert_eq!(rustix::process::getegid().as_raw(), 1000);
    let dir = PathBuf::from(std::env::var_os("KF_REGISTRY_CLIENT_DIR").unwrap());
    let specs: Vec<ExecutionSpec> =
        serde_json::from_slice(&fs::read(dir.join("specs.json")).unwrap()).unwrap();
    let client = FactoryClient::new(PathBuf::from("/var/lib/kf-judge/factory/control.sock"));
    let input = dir.join("inputs");
    let root = ArtifactRoot::open(&input).unwrap();
    if std::env::var("KF_REGISTRY_RESTART").as_deref() == Ok("1") {
        assert!(
            client.execute(specs[0].clone(), &root).is_err(),
            "replay must remain consumed after restart"
        );
        let mut fresh = specs[1].clone();
        fresh.run_id = "registry-after-restart".into();
        assert_eq!(
            bytes(&run(&client, &root, fresh, false).stdout),
            b"python-factory\n"
        );
    } else {
        store(&input.join("source"), b"print('python-factory')\n");
        run(&client, &root, specs[0].clone(), false);
        assert_eq!(
            bytes(&run(&client, &root, specs[1].clone(), false).stdout),
            b"python-factory\n"
        );
        let mut forged = specs[1].clone();
        forged.run_id = "registry-wrong-image".into();
        forged.runtime_identity.as_mut().unwrap().image_digest = "e".repeat(64).try_into().unwrap();
        assert!(
            client.execute(forged, &root).is_err(),
            "unregistered image digest"
        );
        store(&input.join("source"),br#"public class Main { static class Inner {} public static void main(String[] args) throws Exception { if(System.in.read()=='c'){for(int i=0;i<3;i++)new Thread(()->{while(true)try{Thread.sleep(10000);}catch(InterruptedException e){}}).start();System.out.println("live-java");while(true)Thread.sleep(10000);}System.out.println("java-factory");} } class Helper {}"#);
        let compiled = run(&client, &root, specs[2].clone(), false);
        let mut java = specs[3].clone();
        java.inputs.clear();
        assert_eq!(compiled.outputs.len(), 3);
        for (i, class) in compiled.outputs.iter().enumerate() {
            let id = format!("class-{i}");
            store(&input.join(&id), &bytes(&class.file));
            java.inputs.push(InputArtifact {
                artifact_id: id,
                path: class.path.clone(),
            });
        }
        assert_eq!(
            bytes(&run(&client, &root, java.clone(), false).stdout),
            b"java-factory\n"
        );
        store(&input.join("stdin"), b"c");
        java.stdin_artifact = Some("stdin".into());
        java.run_id = "registry-java-deadline".into();
        run(&client, &root, java, true);
        store(&input.join("source"),b"import os,time\nprint('live-python',flush=True)\nos.fork()\nwhile True: time.sleep(10)\n");
        let mut python = specs[1].clone();
        python.run_id = "registry-py-deadline".into();
        run(&client, &root, python, true);
        store(&input.join("source"), b"print('python-factory')\n");
        assert_eq!(
            bytes(&run(&client, &root, specs[4].clone(), false).stdout),
            b"C bytes=0\n"
        );
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if client.stopped().unwrap() {
            break;
        }
        assert!(Instant::now() < deadline, "actual global drain proof");
        std::thread::sleep(Duration::from_millis(20));
    }
    println!("KF_MANAGED_FACTORY_DRAIN_STATUS=0");
}

fn factory(config: &Path, old_inode: Option<u64>) -> Child {
    let mut child = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
        .env_clear()
        .args(["factory", "--config"])
        .arg(config)
        .args(["--lifetime-seconds", "300"])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let socket = Path::new("/var/lib/kf-judge/factory/control.sock");
    let deadline = Instant::now() + Duration::from_secs(45);
    while !fs::metadata(socket)
        .is_ok_and(|s| s.mode() & 0o777 == 0o660 && Some(s.ino()) != old_inode)
    {
        assert!(
            child.try_wait().unwrap().is_none(),
            "factory refused startup"
        );
        assert!(Instant::now() < deadline, "factory publication deadline");
        std::thread::sleep(Duration::from_millis(5));
    }
    child
}
fn controller(dir: &Path, restart: bool) {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .env_clear()
        .env("KF_REGISTRY_CLIENT_DIR", dir)
        .uid(1000)
        .gid(1000)
        .args([
            "--ignored",
            "--exact",
            "ordinary_registry_client",
            "--nocapture",
        ]);
    if restart {
        command.env("KF_REGISTRY_RESTART", "1");
    }
    let output = command.output().unwrap();
    println!("{}", String::from_utf8_lossy(&output.stdout));
    assert!(
        output.status.success(),
        "ordinary client failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("KF_MANAGED_FACTORY_DRAIN_STATUS=0"));
}
#[test]
#[ignore = "default factory mixed images, deadline and restart only in capped no-NIC/no-share VM"]
fn one_factory_managed_registry_deadlines_and_recovery() {
    support::require_vm();
    let mut root = support::Root::new();
    root.config.max_concurrent_runs = 1;
    root.config.identities.count = 1;
    root.config.aggregate.task_cpu_quota_us = 100_000;
    root.config.per_task_ceiling.cpu_time_us = 30_000_000;
    root.config.per_task_ceiling.wall_time_us = 60_000_000;
    for (lang, kind) in [
        ("python313", ImageKind::LinuxCPython313X86_64V1),
        ("java21", ImageKind::LinuxOpenJdk21X86_64V1),
    ] {
        let image_root = format!("/var/lib/kf-judge/images/{lang}");
        let data = fs::read(format!("{image_root}/manifest.json")).unwrap();
        let catalog: ImageCatalog = serde_json::from_slice(&data).unwrap();
        root.config.runtime_images.push(RegisteredImage {
            image_root,
            image_kind: kind,
            runtime_identity: RuntimeIdentity {
                toolchain_id: catalog.toolchain_id,
                image_kind: catalog.image_kind,
                image_revision: catalog.image_revision,
                image_digest: catalog_digest(&data),
                runtime_version: catalog.runtime_version,
            },
            per_task_ceiling: root.config.per_task_ceiling.clone(),
        });
    }
    root.config.clone().validate().unwrap();
    let dir = PathBuf::from(format!("/kf/registry-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o711)).unwrap();
    let inputs = dir.join("inputs");
    fs::create_dir(&inputs).unwrap();
    chown(&inputs, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700)).unwrap();
    store(
        &inputs.join("native"),
        &fs::read("/kf/bin/normal-static").unwrap(),
    );
    let py = &root.config.runtime_images[0];
    let java = &root.config.runtime_images[1];
    let mut jrun = spec(java, "registry-java-run", ExecutionProfile::JavaRun);
    jrun.inputs = vec![InputArtifact {
        artifact_id: "class-0".into(),
        path: "Main.class".into(),
    }];
    let mut native: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    native.run_id = "registry-native".into();
    native.argv.push("empty".into());
    native.stdin_artifact = None;
    native.inputs[0].artifact_id = "native".into();
    store(
        &dir.join("specs.json"),
        &serde_json::to_vec(&vec![
            spec(py, "registry-py-check", ExecutionProfile::PythonCheck),
            spec(py, "registry-py-run", ExecutionProfile::PythonRun),
            spec(java, "registry-java-compile", ExecutionProfile::JavaCompile),
            jrun,
            native,
        ])
        .unwrap(),
    );
    let config = dir.join("runtime.json");
    fs::write(&config, serde_json::to_vec(&root.config).unwrap()).unwrap();
    fs::set_permissions(&config, fs::Permissions::from_mode(0o600)).unwrap();
    let mut process = factory(&config, None);
    controller(&dir, false);
    let old_inode = fs::metadata("/var/lib/kf-judge/factory/control.sock")
        .unwrap()
        .ino();
    process.kill().unwrap();
    process.wait().unwrap();
    // Unknown actual identity contents must survive restart recovery unchanged.
    let unknown = Path::new("/var/lib/kf-judge/identities/uid-100000");
    fs::create_dir(unknown).unwrap();
    fs::set_permissions(unknown, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(unknown.join("foreign"), b"unknown-canary").unwrap();
    let metadata = fs::metadata(unknown).unwrap();
    let refused = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
        .env_clear()
        .args(["factory", "--config"])
        .arg(&config)
        .args(["--lifetime-seconds", "1"])
        .output()
        .unwrap();
    assert_eq!(refused.status.code(), Some(2));
    assert!(refused.stdout.is_empty());
    assert_eq!(fs::metadata(unknown).unwrap().ino(), metadata.ino());
    assert_eq!(
        fs::read(unknown.join("foreign")).unwrap(),
        b"unknown-canary"
    );
    println!("KF_MANAGED_FACTORY_UNKNOWN_PRESERVED=1");
    fs::remove_file(unknown.join("foreign")).unwrap();
    fs::remove_dir(unknown).unwrap();
    let mut restarted = factory(&config, Some(old_inode));
    controller(&dir, true);
    restarted.kill().unwrap();
    restarted.wait().unwrap();
    for name in ["tasks", "captures", "identities"] {
        assert_eq!(
            fs::read_dir(format!("/var/lib/kf-judge/{name}"))
                .unwrap()
                .count(),
            0
        );
    }
    assert!(
        !fs::read_dir(&root.path)
            .unwrap()
            .any(|e| e.unwrap().file_type().unwrap().is_dir())
    );
    // Only fixture-owned factory files are removed, after positive global drain.
    for name in ["requests", "socket.json", "control.sock", "lock"] {
        fs::remove_file(Path::new("/var/lib/kf-judge/factory").join(name)).unwrap();
    }
    fs::remove_dir("/var/lib/kf-judge/factory").unwrap();
    fs::remove_dir_all(&dir).unwrap();
    println!("KF_MANAGED_FACTORY_REGISTRY_STATUS=0");
}
