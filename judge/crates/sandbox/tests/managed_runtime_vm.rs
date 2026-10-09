#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
//! Actual new profile execution through the ordinary default Root entry.
//! This does not substitute for the later ordinary Worker/browser pipeline.
#[allow(dead_code)]
mod support;
use kf_contracts::judging_v2::RuntimeIdentity;
use kf_sandbox::{
    image::{ImageCatalog, catalog_digest},
    profiles,
    runtime::ImageKind,
    spec::*,
};
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::PathBuf,
    process::{Command, Stdio},
};

struct Fixture {
    root: support::Root,
    dir: PathBuf,
    config: PathBuf,
}
impl Fixture {
    fn new(language: &str, kind: ImageKind) -> Self {
        support::require_vm();
        let mut root = support::Root::new();
        root.config.max_concurrent_runs = 1;
        root.config.identities.count = 1;
        root.config.aggregate.task_cpu_quota_us = 100_000;
        root.config.per_task_ceiling.cpu_time_us = 30_000_000;
        root.config.per_task_ceiling.wall_time_us = 60_000_000;
        root.config.image_root = format!("/var/lib/kf-judge/images/{language}");
        root.config.image_kind = kind;
        let bytes = fs::read(format!("{}/manifest.json", root.config.image_root)).unwrap();
        let catalog: ImageCatalog = serde_json::from_slice(&bytes).unwrap();
        root.config.runtime_identity = Some(RuntimeIdentity {
            toolchain_id: catalog.toolchain_id.clone(),
            image_kind: catalog.image_kind,
            image_revision: catalog.image_revision,
            image_digest: catalog_digest(&bytes),
            runtime_version: catalog.runtime_version.clone(),
        });
        root.config.clone().validate().unwrap();
        catalog
            .validate(root.config.runtime_identity.as_ref().unwrap())
            .unwrap();
        let image = PathBuf::from(&root.config.image_root);
        for (path, mode, bytes) in std::iter::once((String::new(), 0o555, None))
            .chain(catalog.directories.iter().map(|p| (p.clone(), 0o555, None)))
            .chain(catalog.files.iter().map(|p| {
                (
                    p.path.clone(),
                    if p.executable { 0o555 } else { 0o444 },
                    Some(p.bytes),
                )
            }))
        {
            let metadata = fs::symlink_metadata(image.join(&path)).unwrap();
            assert_eq!(
                (metadata.uid(), metadata.gid(), metadata.mode() & 0o7777),
                (0, 0, mode),
                "image metadata {path}"
            );
            if let Some(bytes) = bytes {
                assert!(metadata.is_file(), "{path}");
                assert_eq!(metadata.nlink(), 1, "{path}");
                assert_eq!(metadata.len(), bytes, "{path}");
            }
        }
        println!("KF_MANAGED_CATALOG_METADATA_STATUS=0 {language}");
        let dir = PathBuf::from(format!("/kf/managed-{}-{language}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        for name in ["inputs", "outputs"] {
            fs::create_dir(dir.join(name)).unwrap();
        }
        let config = dir.join("runtime.json");
        let fixture = Self { root, dir, config };
        fixture.save_config();
        println!(
            "KF_MANAGED_IMAGE_PROOF {}",
            serde_json::json!({"language":language,"identity":fixture.root.config.runtime_identity})
        );
        fixture
    }
    fn save_config(&self) {
        fs::write(&self.config, serde_json::to_vec(&self.root.config).unwrap()).unwrap();
        fs::set_permissions(&self.config, fs::Permissions::from_mode(0o600)).unwrap();
    }
    fn input(&self, name: &str, bytes: &[u8]) {
        fs::write(self.dir.join("inputs").join(name), bytes).unwrap();
        fs::set_permissions(
            self.dir.join("inputs").join(name),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
    }
    fn request(&self, id: &str, profile: ExecutionProfile) -> ExecutionSpec {
        let mut limits = ResourceLimits::for_profile(profile);
        // Same finite acceptance ceiling as the existing real GNU regression;
        // this fixture does not change any submitted job's configured limits.
        limits.cpu_time_us = 30_000_000;
        limits.wall_time_us = 60_000_000;
        ExecutionSpec {
            schema_version: kf_contracts::SchemaVersion,
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
    fn command(&self, spec: &ExecutionSpec) -> Command {
        fs::write(
            self.dir.join("spec.json"),
            serde_json::to_vec(spec).unwrap(),
        )
        .unwrap();
        let mut command = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap());
        command
            .env_clear()
            .args(["run", "--config"])
            .arg(&self.config)
            .arg("--spec")
            .arg(self.dir.join("spec.json"))
            .arg("--artifacts")
            .arg(self.dir.join("inputs"))
            .arg("--output")
            .arg(self.dir.join("outputs"));
        command
    }
    fn execute(&self, spec: &ExecutionSpec) -> serde_json::Value {
        let output = if std::env::var("KF_MANAGED_TRACE").as_deref() == Ok("1") {
            let mut child = self
                .command(spec)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap();
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
            let mut tracer = None;
            while child.try_wait().unwrap().is_none() && std::time::Instant::now() < deadline {
                let mut parents = vec![child.id()];
                for _ in 0..4 {
                    let mut children = vec![];
                    for pid in parents {
                        if let Ok(data) =
                            fs::read_to_string(format!("/proc/{pid}/task/{pid}/children"))
                        {
                            children.extend(
                                data.split_whitespace()
                                    .filter_map(|v| v.parse::<u32>().ok()),
                            );
                        }
                    }
                    if let Some(pid) = children.iter().find(|pid| {
                        fs::read_to_string(format!("/proc/{pid}/comm"))
                            .is_ok_and(|s| s.trim() == "python3.13")
                    }) {
                        tracer = Some(
                            Command::new("/kf/bin/strace")
                                .env_clear()
                                .args([
                                    "-ff",
                                    "-qq",
                                    "-e",
                                    "trace=open,openat,openat2,fcntl,ioctl",
                                    "-e",
                                    "status=failed",
                                    "-o",
                                ])
                                .arg(self.dir.join("trace"))
                                .arg("-p")
                                .arg(pid.to_string())
                                .spawn()
                                .unwrap(),
                        );
                        break;
                    }
                    parents = children;
                }
                if tracer.is_some() {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
            let output = child.wait_with_output().unwrap();
            if let Some(mut tracer) = tracer {
                let _ = tracer.wait();
            }
            output
        } else {
            self.command(spec).output().unwrap()
        };
        if std::env::var("KF_MANAGED_TRACE").as_deref() == Ok("1") {
            for entry in fs::read_dir(&self.dir).unwrap() {
                let entry = entry.unwrap();
                if entry.file_name().to_string_lossy().starts_with("trace.") {
                    let data = fs::read_to_string(entry.path()).unwrap();
                    for line in data.lines().filter(|s| s.contains("EPERM")) {
                        println!(
                            "KF_MANAGED_DENIED_SYSCALL {}",
                            line.chars().take(1024).collect::<String>()
                        );
                    }
                }
            }
        }
        assert!(
            output.status.success(),
            "{} {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        println!(
            "KF_MANAGED_RUN_PROOF {}",
            serde_json::json!({"spec":spec,"result":value})
        );
        for name in ["stdout", "stderr"] {
            if let Ok(bytes) = fs::read(self.dir.join("outputs").join(&spec.run_id).join(name)) {
                println!(
                    "KF_MANAGED_CAPTURE {}",
                    serde_json::json!({"run_id":spec.run_id,"name":name,"prefix":String::from_utf8_lossy(&bytes[..bytes.len().min(4096)])})
                );
            }
        }
        assert_eq!(value["report"]["cleanup"], "complete");
        assert_eq!(value["report"]["errors"], serde_json::json!([]));
        self.empty();
        value
    }
    fn reject(&self, spec: &ExecutionSpec, code: &str) {
        let output = self.command(spec).output().unwrap();
        assert_eq!(
            output.status.code(),
            Some(2),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(output.stdout.is_empty());
        let value: serde_json::Value = serde_json::from_slice(&output.stderr).unwrap();
        assert_eq!(value["error"], code);
        println!("KF_MANAGED_REFUSAL_PROOF {value}");
        self.empty();
    }
    fn stdout(&self, id: &str) -> Vec<u8> {
        fs::read(self.dir.join("outputs").join(id).join("stdout")).unwrap()
    }
    fn image_refusals(&mut self, spec: &ExecutionSpec, binary: &str) {
        let image = PathBuf::from(&self.root.config.image_root);
        let executable = image.join(binary);
        let backup = self.dir.join("trusted-runtime-backup");
        fs::rename(&executable, &backup).unwrap();
        self.reject(spec, "runtime_not_ready");
        fs::rename(&backup, &executable).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o777)).unwrap();
        self.reject(spec, "runtime_not_ready");
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o555)).unwrap();
        let original = self.root.config.runtime_identity.clone().unwrap();
        self.root
            .config
            .runtime_identity
            .as_mut()
            .unwrap()
            .runtime_version = "1.0.0".to_owned().try_into().unwrap();
        self.save_config();
        self.reject(spec, "untrusted_runtime_config");
        self.root.config.runtime_identity = Some(original.clone());
        self.root
            .config
            .runtime_identity
            .as_mut()
            .unwrap()
            .image_digest = "e".repeat(64).try_into().unwrap();
        self.save_config();
        self.reject(spec, "runtime_not_ready");
        self.root.config.runtime_identity = Some(original);
        self.save_config();
        let loader = image.join("lib64/ld-linux-x86-64.so.2");
        fs::rename(&loader, &backup).unwrap();
        self.reject(spec, "runtime_not_ready");
        fs::rename(&backup, &loader).unwrap();
        let original = fs::read(&loader).unwrap();
        let mut corrupted = original.clone();
        let last = corrupted.len() - 1;
        corrupted[last] ^= 1;
        fs::write(&loader, corrupted).unwrap();
        self.reject(spec, "runtime_not_ready");
        fs::write(&loader, original).unwrap();
        println!(
            "KF_MANAGED_IMAGE_REFUSALS_STATUS=0 {}",
            self.root.config.image_root
        );
    }
    fn empty(&self) {
        for name in ["tasks", "captures", "identities"] {
            let p = PathBuf::from("/var/lib/kf-judge").join(name);
            if p.exists() {
                assert_eq!(fs::read_dir(p).unwrap().count(), 0);
            }
        }
        assert!(
            !fs::read_dir(&self.root.path).unwrap().any(|e| e
                .unwrap()
                .file_type()
                .unwrap()
                .is_dir())
        );
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

#[test]
#[ignore = "independent no-NIC/no-share VM with read-back total and fixture caps"]
fn python_profile_check_run_and_identity_refusals() {
    let mut f = Fixture::new("python313", ImageKind::LinuxCPython313X86_64V1);
    f.input("source", b"import sys\nprint(sys.version.split()[0])\nprint('top-level')\nopen('marker','w').write('owned')\n");
    let check = f.request("py-check", ExecutionProfile::PythonCheck);
    let r = f.execute(&check);
    assert_eq!(r["outcome"], serde_json::json!({"kind":"exited","code":0}));
    assert!(f.stdout("py-check").is_empty());
    assert!(r["report"]["artifacts"].as_array().unwrap().is_empty());
    let run = f.request("py-run", ExecutionProfile::PythonRun);
    let r = f.execute(&run);
    assert_eq!(r["outcome"], serde_json::json!({"kind":"exited","code":0}));
    assert_eq!(f.stdout("py-run"), b"3.13.16\ntop-level\n");
    f.input("source", b"# coding: latin-1\nprint('caf\xe9')\n");
    assert_eq!(
        f.execute(&f.request("py-encoding", ExecutionProfile::PythonCheck))["outcome"]["code"],
        0
    );
    f.input("source", b"if !invalid:\n    pass\n");
    assert_eq!(
        f.execute(&f.request("py-syntax", ExecutionProfile::PythonCheck))["outcome"]["code"],
        1
    );
    let mut wrong = run.clone();
    wrong.argv.push("-c".into());
    wrong.run_id = "py-argv-refused".into();
    f.reject(&wrong, "invalid_execution_spec");
    f.image_refusals(&run, "usr/bin/python3.13");
}

#[test]
#[ignore = "independent no-NIC/no-share VM with read-back total and fixture caps"]
fn java_profile_compiles_class_set_and_runs_same_set() {
    let mut f = Fixture::new("java21", ImageKind::LinuxOpenJdk21X86_64V1);
    f.input("source", br#"public class Main { static class Inner { } public static void main(String[] args) { System.out.println(System.getProperty("java.runtime.version")); Runnable r = new Runnable() { public void run() { System.out.println("java-ok"); } }; r.run(); new Inner(); } } class Helper {}"#);
    let compile = f.request("java-compile", ExecutionProfile::JavaCompile);
    let r = f.execute(&compile);
    assert_eq!(r["outcome"], serde_json::json!({"kind":"exited","code":0}));
    let artifacts = r["report"]["artifacts"].as_array().unwrap();
    let names: Vec<_> = artifacts
        .iter()
        .map(|a| a["path"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        [
            "Helper.class",
            "Main$1.class",
            "Main$Inner.class",
            "Main.class"
        ]
    );
    let mut run = f.request("java-run", ExecutionProfile::JavaRun);
    run.inputs.clear();
    for (i, name) in names.iter().enumerate() {
        let id = format!("class-{i}");
        let bytes = fs::read(
            f.dir
                .join("outputs/java-compile")
                .join(format!("artifact-{i}")),
        )
        .unwrap();
        f.input(&id, &bytes);
        run.inputs.push(InputArtifact {
            artifact_id: id,
            path: (*name).into(),
        });
    }
    let r = f.execute(&run);
    assert_eq!(r["outcome"], serde_json::json!({"kind":"exited","code":0}));
    assert_eq!(f.stdout("java-run"), b"21.0.12.1+1-LTS\njava-ok\n");
    f.input("source", b"public class Main { invalid }\n");
    let r = f.execute(&f.request("java-syntax", ExecutionProfile::JavaCompile));
    assert_eq!(r["outcome"]["code"], 1);
    assert!(r["report"]["artifacts"].as_array().unwrap().is_empty());
    let mut wrong = run;
    wrong
        .env
        .insert("JAVA_TOOL_OPTIONS".into(), "-javaagent:/work/x.jar".into());
    wrong.run_id = "java-env-refused".into();
    f.reject(&wrong, "invalid_execution_spec");
    f.image_refusals(&wrong, "usr/lib/jvm/bin/java");
}
