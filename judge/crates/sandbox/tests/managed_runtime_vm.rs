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
            runtime_identity: self.root.config.runtime_identity.clone(),
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
    fn java_program(&self, source: &[u8], id: &str) -> ExecutionSpec {
        self.input("source", source);
        let compile = self.request(id, ExecutionProfile::JavaCompile);
        let result = self.execute(&compile);
        assert_eq!(
            result["outcome"],
            serde_json::json!({"kind":"exited","code":0})
        );
        let mut run = self.request("java-bound-run", ExecutionProfile::JavaRun);
        run.inputs.clear();
        for (index, artifact) in result["report"]["artifacts"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
        {
            let id = format!("class-{index}");
            self.input(
                &id,
                &fs::read(
                    self.dir
                        .join("outputs")
                        .join(&compile.run_id)
                        .join(format!("artifact-{index}")),
                )
                .unwrap(),
            );
            run.inputs.push(InputArtifact {
                artifact_id: id,
                path: artifact["path"].as_str().unwrap().into(),
            });
        }
        run
    }
    fn cancel_through_ordinary_peer(&self, spec: &ExecutionSpec) {
        // The independent fixture controller is a bounded ordinary UID client.
        // Only the default Root helper performs privileged task operations.
        fs::set_permissions(&self.dir, fs::Permissions::from_mode(0o711)).unwrap();
        let inputs = self.dir.join("inputs");
        std::os::unix::fs::chown(&inputs, Some(1000), Some(1000)).unwrap();
        fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700)).unwrap();
        for entry in fs::read_dir(&inputs).unwrap() {
            let path = entry.unwrap().path();
            std::os::unix::fs::chown(&path, Some(1000), Some(1000)).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let wire = self.dir.join("wire.json");
        fs::write(
            &wire,
            serde_json::to_vec(&serde_json::json!({"spec":spec})).unwrap(),
        )
        .unwrap();
        std::os::unix::fs::chown(&wire, Some(1000), Some(1000)).unwrap();
        fs::set_permissions(&wire, fs::Permissions::from_mode(0o600)).unwrap();
        let socket = self.dir.join("socket");
        let mut helper = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
            .env_clear()
            .args(["serve-one", "--config"])
            .arg(&self.config)
            .arg("--socket")
            .arg(&socket)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        while !fs::metadata(&socket).is_ok_and(|m| m.mode() & 0o777 == 0o660) {
            assert!(
                helper.try_wait().unwrap().is_none(),
                "helper failed before peer publication"
            );
            assert!(
                std::time::Instant::now() < deadline,
                "bounded helper publication"
            );
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        let client = Command::new("/kf/bin/managed-disconnect")
            .env_clear()
            .arg(&socket)
            .arg(&inputs)
            .arg(&wire)
            .output()
            .unwrap();
        assert!(client.status.success(), "ordinary controller failed");
        let output = helper.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let result: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(result["report"]["cleanup"], "complete");
        assert_eq!(result["report"]["errors"], serde_json::json!([]));
        assert_eq!(result["report"]["limits"], serde_json::json!([]));
        assert!(
            result["report"]["cancelled_at_wall_time_us"]
                .as_u64()
                .unwrap()
                > 0
        );
        // Marker comes from actual user main after runtime bootstrap, so a
        // cancelled preparation alone cannot pass this execution test.
        assert!(
            result["report"]["stdout"]["retained_bytes"]
                .as_u64()
                .unwrap()
                >= 5
        );
        assert!(!socket.exists());
        self.empty();
        println!(
            "KF_MANAGED_PEER_CANCEL_PROOF {}",
            serde_json::json!({"ordinary_uid":1000,"profile":spec.profile,"identity":spec.runtime_identity,"spec":spec,"result":result,"owned_resources_zero":true})
        );
    }
    fn image_refusals(&mut self, spec: &ExecutionSpec, binary: &str) {
        self.identity_refusals(spec);
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
    fn identity_refusals(&self, spec: &ExecutionSpec) {
        let mut wrong = spec.clone();
        wrong.runtime_identity = None;
        self.reject(&wrong, "invalid_execution_spec");
        let mut wrong = spec.clone();
        wrong.runtime_identity.as_mut().unwrap().image_digest = "d".repeat(64).try_into().unwrap();
        self.reject(&wrong, "invalid_execution_spec");
        let mut wrong = spec.clone();
        wrong.runtime_identity.as_mut().unwrap().runtime_version =
            "1.0.0".to_owned().try_into().unwrap();
        self.reject(&wrong, "invalid_execution_spec");
        println!("KF_MANAGED_REQUEST_IDENTITY_STATUS=0");
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

#[test]
#[ignore = "malicious and overlimit code only in an independent no-NIC/no-share capped VM"]
fn python_isolation_and_kernel_limits_have_actual_evidence() {
    let f = Fixture::new("python313", ImageKind::LinuxCPython313X86_64V1);
    f.input("source", br#"import ctypes, errno, os, socket
libc = ctypes.CDLL(None, use_errno=True)
assert os.geteuid() != 0
for number, args in [(41,(2,1,0)),(101,(0,0,0,0)),(165,(0,0,0,0,0)),(308,(-1,0)),(272,(0x10000000,)),(321,(0,0,0)),(126,(0,0)),(62,(os.getpid(),0))]:
    ctypes.set_errno(0)
    assert libc.syscall(number,*args) == -1, number
    assert ctypes.get_errno() == errno.EPERM, (number,ctypes.get_errno())
try:
    socket.socket()
    raise AssertionError('network allowed')
except PermissionError:
    pass
for path in ['/kf/INDEPENDENT_VM','/var/lib/kf-judge','/root/.ssh','/home/kronecker','/proc/1/root/kf/INDEPENDENT_VM']:
    assert not os.path.exists(path), path
try:
    open('/usr/lib/python3.13/os.py','wb')
    raise AssertionError('image writable')
except OSError:
    pass
print('python-isolation-ok')
"#);
    let result = f.execute(&f.request("py-isolation", ExecutionProfile::PythonRun));
    assert_eq!(
        result["outcome"],
        serde_json::json!({"kind":"exited","code":0})
    );
    assert_eq!(f.stdout("py-isolation"), b"python-isolation-ok\n");
    for (id, source, primary) in [
        ("py-cpu", b"print('cpu-ready',flush=True)\nwhile True: pass\n".as_slice(), "cpu_time"),
        ("py-memory", b"print('memory-ready',flush=True)\na=[]\nwhile True: a.append(bytearray(8*1024*1024))\n".as_slice(), "memory"),
        ("py-output", b"import os\nwhile True: os.write(1,b'x'*65536)\n".as_slice(), "stdout"),
    ] {
        f.input("source", source);
        let mut spec = f.request(id, ExecutionProfile::PythonRun);
        if primary == "cpu_time" {
            spec.limits.cpu_time_us = 2_000_000;
            spec.limits.wall_time_us = 10_000_000;
        }
        spec.limits.stdout_bytes = 4096;
        let result = f.execute(&spec);
        assert_eq!(result["outcome"], serde_json::json!({"kind":"limit_exceeded","primary":primary}));
        assert!(result["report"]["usage"]["cpu_time_us"]["value"].as_u64().is_some());
        assert!(result["report"]["usage"]["memory_peak_bytes"]["value"].as_u64().is_some());
        if primary == "memory" {
            assert!(result["report"]["usage"]["memory_events"]["value"]["oom_kill"].as_u64().unwrap() > 0);
        }
    }
    f.input(
        "source",
        b"raise MemoryError('OutOfMemoryError: user text')\n",
    );
    let result = f.execute(&f.request("py-user-oom", ExecutionProfile::PythonRun));
    assert_eq!(result["outcome"]["code"], 1);
    assert_eq!(result["report"]["limits"], serde_json::json!([]));
    assert_eq!(
        result["report"]["usage"]["memory_events"]["value"]["oom_kill"],
        0
    );
}

#[test]
#[ignore = "JVM security, threads and real cgroup OOM only in an independent capped VM"]
fn java_isolation_and_whole_jvm_limits_have_actual_evidence() {
    let f = Fixture::new("java21", ImageKind::LinuxOpenJdk21X86_64V1);
    let program = f.java_program(br#"import java.io.*; import java.net.*; import java.nio.file.*; import java.lang.reflect.*; import sun.misc.Unsafe;
public class Main {
 public static void main(String[] args) throws Exception {
  String mode = new BufferedReader(new InputStreamReader(System.in)).readLine();
  if (mode.equals("normal")) {
   if (Runtime.getRuntime().availableProcessors()!=1) throw new AssertionError("cpu ergonomics");
   if (Runtime.getRuntime().maxMemory()>224L*1024*1024) throw new AssertionError("heap ergonomics");
   try { new ServerSocket(0); throw new AssertionError("network allowed"); } catch(IOException | ExceptionInInitializerError expected) { Throwable cause=expected; while(cause.getCause()!=null) cause=cause.getCause(); if(!(cause instanceof IOException) || !cause.getMessage().contains("Operation not permitted")) throw expected; }
   for(String p:new String[]{"/kf/INDEPENDENT_VM","/var/lib/kf-judge","/root/.ssh","/home/kronecker"}) if(Files.exists(Path.of(p))) throw new AssertionError("host visible");
   try { Files.write(Path.of("/usr/lib/jvm/release"),new byte[]{1}); throw new AssertionError("image writable"); } catch(IOException expected) {}
   System.out.println("java-isolation-ok");
  } else if(mode.equals("fake-oom")) { throw new OutOfMemoryError("user controlled");
  } else if(mode.equals("cpu")) { System.out.println("cpu-ready"); long x=0; while(true) x=Math.multiplyExact(x,1)+1;
  } else if(mode.equals("memory")) {
   System.out.println("memory-ready"); Field field=Unsafe.class.getDeclaredField("theUnsafe"); field.setAccessible(true); Unsafe unsafe=(Unsafe)field.get(null);
   while(true) { long p=unsafe.allocateMemory(16L*1024*1024); unsafe.setMemory(p,16L*1024*1024,(byte)1); }
  } else if(mode.equals("output")) { byte[] bytes=new byte[65536]; while(true) System.out.write(bytes);
  } else if(mode.equals("threads")) { while(true) new Thread(()->{try{Thread.sleep(60000);}catch(InterruptedException e){}}).start();
  } else { throw new AssertionError("mode"); }
 }
}"#, "java-security-compile");
    for (id, mode, primary) in [
        ("java-isolation", "normal", None),
        ("java-user-oom", "fake-oom", None),
        ("java-cpu", "cpu", Some("cpu_time")),
        ("java-memory", "memory", Some("memory")),
        ("java-output", "output", Some("stdout")),
        ("java-threads", "threads", Some("processes")),
    ] {
        f.input("stdin", format!("{mode}\n").as_bytes());
        let mut spec = program.clone();
        spec.run_id = id.into();
        spec.stdin_artifact = Some("stdin".into());
        if mode == "cpu" {
            spec.limits.cpu_time_us = 5_000_000;
            spec.limits.wall_time_us = 15_000_000;
        }
        spec.limits.stdout_bytes = 4096;
        if mode == "threads" {
            spec.limits.processes = 16;
        }
        spec.argv = profiles::argv(spec.profile, &spec.limits).unwrap();
        let result = f.execute(&spec);
        if let Some(primary) = primary {
            assert_eq!(
                result["outcome"],
                serde_json::json!({"kind":"limit_exceeded","primary":primary})
            );
        } else {
            assert_eq!(
                result["outcome"]["code"],
                if mode == "normal" { 0 } else { 1 }
            );
            assert_eq!(result["report"]["limits"], serde_json::json!([]));
            assert_eq!(
                result["report"]["usage"]["memory_events"]["value"]["oom_kill"],
                0
            );
        }
        if mode == "normal" {
            assert_eq!(f.stdout(id), b"java-isolation-ok\n");
        }
        if mode == "memory" {
            assert!(
                result["report"]["usage"]["memory_events"]["value"]["oom_kill"]
                    .as_u64()
                    .unwrap()
                    > 0
            );
        }
    }
}

#[test]
#[ignore = "live Python fork tree cancellation only in independent no-NIC/no-share capped VM"]
fn python_live_fork_tree_peer_cancellation_is_drained() {
    let f = Fixture::new("python313", ImageKind::LinuxCPython313X86_64V1);
    f.input(
        "source",
        b"import os,time\nprint('py-live',flush=True)\nos.fork()\nwhile True: time.sleep(10)\n",
    );
    f.cancel_through_ordinary_peer(&f.request("py-peer-cancel", ExecutionProfile::PythonRun));
}

#[test]
#[ignore = "live Java thread tree cancellation only in independent no-NIC/no-share capped VM"]
fn java_live_threads_peer_cancellation_is_drained() {
    let f = Fixture::new("java21", ImageKind::LinuxOpenJdk21X86_64V1);
    let run = f.java_program(br#"public class Main { public static void main(String[] args) throws Exception { for(int i=0;i<3;i++)new Thread(()->{while(true)try{Thread.sleep(10000);}catch(InterruptedException e){}}).start(); System.out.println("java-live"); while(true)Thread.sleep(10000); } }"#, "java-cancel-compile");
    f.cancel_through_ordinary_peer(&run);
}

#[test]
#[ignore = "file exhaustion and live wall deadline only in an independent capped VM"]
fn python_files_scratch_and_wall_deadline_are_bounded() {
    let f = Fixture::new("python313", ImageKind::LinuxCPython313X86_64V1);
    f.input(
        "source",
        br#"import errno, os, resource
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (32,32)
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (1048576,1048576)
fds=[]
try:
    for i in range(128): fds.append(os.open('/dev/null',os.O_RDONLY))
    raise AssertionError('unbounded descriptors')
except OSError as e:
    assert e.errno == errno.EMFILE, e
finally:
    for fd in fds: os.close(fd)
print('fd-emfile',len(fds),flush=True)
fd=os.open('/work/large',os.O_CREAT|os.O_WRONLY,0o600)
try:
    for i in range(32): os.write(fd,b'x'*65536)
    raise AssertionError('unbounded file')
except OSError as e:
    assert e.errno == errno.EFBIG, e
finally:
    os.close(fd)
assert os.stat('/work/large').st_size == 1048576
os.unlink('/work/large')
print('file-efbig-1048576',flush=True)
for path,cap in [('/tmp',2097152),('/work',6291456)]:
    s=os.statvfs(path)
    assert s.f_blocks*s.f_frsize == cap, (path,s)
    total=0
    try:
        for i in range(256):
            fd=os.open(path+'/quota-'+str(i),os.O_CREAT|os.O_WRONLY,0o600)
            try: total+=os.write(fd,b'x'*65536)
            finally: os.close(fd)
        raise AssertionError('unbounded scratch')
    except OSError as e:
        assert e.errno == errno.ENOSPC, e
    assert 0 < total <= cap, (path,total)
    print('scratch-enospc',path,total,cap,flush=True)
"#,
    );
    let mut spec = f.request("py-files-scratch", ExecutionProfile::PythonRun);
    spec.limits.open_files = 32;
    spec.limits.file_bytes = 1 << 20;
    spec.limits.workdir_bytes = 8 << 20;
    let result = f.execute(&spec);
    assert_eq!(
        result["outcome"],
        serde_json::json!({"kind":"exited","code":0})
    );
    assert_eq!(result["report"]["limits"], serde_json::json!([]));
    let stdout = String::from_utf8(f.stdout(&spec.run_id)).unwrap();
    for marker in [
        "fd-emfile",
        "file-efbig-1048576",
        "scratch-enospc /tmp",
        "scratch-enospc /work",
    ] {
        assert!(stdout.contains(marker), "{stdout}");
    }
    f.input("source", b"import os\nassert not os.path.exists('/work/large')\nassert not any(p.startswith('quota-') for d in ['/work','/tmp'] for p in os.listdir(d))\nprint('fresh-root')\n");
    spec.run_id = "py-files-fresh".into();
    assert_eq!(f.execute(&spec)["outcome"]["code"], 0);
    assert_eq!(f.stdout(&spec.run_id), b"fresh-root\n");
    f.input(
        "source",
        b"import os,time\nprint('wall-live',flush=True)\nos.fork()\nwhile True: time.sleep(10)\n",
    );
    spec.run_id = "py-wall-tree".into();
    spec.limits.wall_time_us = 3_000_000;
    let result = f.execute(&spec);
    assert_eq!(
        result["outcome"],
        serde_json::json!({"kind":"limit_exceeded","primary":"wall_time"})
    );
    assert!(f.stdout(&spec.run_id).starts_with(b"wall-live\n"));
    println!("KF_MANAGED_FILE_WALL_STATUS=0 python313");
}

#[test]
#[ignore = "JVM file exhaustion and live wall deadline only in an independent capped VM"]
fn java_files_scratch_and_wall_deadline_are_bounded() {
    let f = Fixture::new("java21", ImageKind::LinuxOpenJdk21X86_64V1);
    let program = f.java_program(br#"import java.io.*; import java.nio.file.*; import java.util.*; import sun.misc.Signal; import sun.misc.SignalHandler;
public class Main {
 public static void main(String[] args) throws Exception {
  String mode=new BufferedReader(new InputStreamReader(System.in)).readLine();
  if(mode.equals("files")) {
   String limits=Files.readString(Path.of("/proc/self/limits"));
   if(!limits.matches("(?s).*Max open files +64 +64 +files.*") || !limits.matches("(?s).*Max file size +1048576 +1048576 +bytes.*"))throw new AssertionError(limits);
   ArrayList<FileInputStream> fds=new ArrayList<>();
   try { for(int i=0;i<128;i++)fds.add(new FileInputStream("/dev/null")); throw new AssertionError("unbounded fds"); }
   catch(IOException e) { if(!e.getMessage().contains("Too many open files"))throw e; }
   finally { for(FileInputStream fd:fds)fd.close(); }
   System.out.println("fd-emfile "+fds.size());
   Signal.handle(new Signal("XFSZ"),SignalHandler.SIG_IGN);
   byte[] block=new byte[65536];
   try(FileOutputStream out=new FileOutputStream("/work/large")) {
    try { for(int i=0;i<32;i++)out.write(block); throw new AssertionError("unbounded file"); }
    catch(IOException e) { if(!e.getMessage().contains("File too large"))throw e; }
   }
   if(Files.size(Path.of("/work/large"))!=1048576)throw new AssertionError("file size");
   Files.delete(Path.of("/work/large"));
   System.out.println("file-efbig-1048576");
   for(String path:new String[]{"/tmp","/work"}) {
    long cap=path.equals("/tmp")?2097152:6291456;
    if(new File(path).getTotalSpace()!=cap)throw new AssertionError("scratch cap");
    int total=0;
    try { for(int i=0;i<256;i++)try(FileOutputStream out=new FileOutputStream(path+"/quota-"+i)){out.write(block);total+=block.length;} throw new AssertionError("unbounded scratch"); }
    catch(IOException e) { if(!e.getMessage().contains("No space left on device"))throw e; }
    if(total<=0 || total>cap)throw new AssertionError("scratch bytes");
    System.out.println("scratch-enospc "+path+" "+total+" "+cap);
   }
  } else if(mode.equals("fresh")) {
   for(String path:new String[]{"/work","/tmp"})for(String name:new File(path).list())if(name.equals("large") || name.startsWith("quota-"))throw new AssertionError("stale files");
   System.out.println("fresh-root");
  } else if(mode.equals("wall")) {
   for(int i=0;i<3;i++)new Thread(()->{while(true)try{Thread.sleep(10000);}catch(InterruptedException e){}}).start();
   System.out.println("wall-live");while(true)Thread.sleep(10000);
  } else throw new AssertionError("mode");
 }
}"#, "java-files-compile");
    for (id, mode) in [
        ("java-files-scratch", "files"),
        ("java-files-fresh", "fresh"),
        ("java-wall-tree", "wall"),
    ] {
        f.input("stdin", format!("{mode}\n").as_bytes());
        let mut spec = program.clone();
        spec.run_id = id.into();
        spec.stdin_artifact = Some("stdin".into());
        spec.limits.open_files = 64;
        spec.limits.file_bytes = 1 << 20;
        spec.limits.workdir_bytes = 8 << 20;
        if mode == "wall" {
            spec.limits.wall_time_us = 8_000_000;
        }
        let result = f.execute(&spec);
        let stdout = String::from_utf8(f.stdout(id)).unwrap();
        if mode == "wall" {
            assert_eq!(
                result["outcome"],
                serde_json::json!({"kind":"limit_exceeded","primary":"wall_time"})
            );
            assert!(stdout.starts_with("wall-live\n"));
        } else {
            assert_eq!(
                result["outcome"],
                serde_json::json!({"kind":"exited","code":0})
            );
            assert_eq!(result["report"]["limits"], serde_json::json!([]));
            if mode == "fresh" {
                assert_eq!(stdout, "fresh-root\n");
            } else {
                for marker in [
                    "fd-emfile",
                    "file-efbig-1048576",
                    "scratch-enospc /tmp",
                    "scratch-enospc /work",
                ] {
                    assert!(stdout.contains(marker), "{stdout}");
                }
            }
        }
    }
    println!("KF_MANAGED_FILE_WALL_STATUS=0 java21");
}
