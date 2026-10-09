#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
//! Real ordinary Worker/Engine and default Root, only in the independent VM.
#[allow(dead_code)]
mod support;
use kf_contracts::{judging as v1, judging_v2 as v2};
use kf_sandbox::{
    factory::FactoryClient,
    image::{ImageCatalog, catalog_digest},
    runtime::{ImageKind, RegisteredImage},
    spec::{ExecutionProfile, ResourceLimits},
};
use sha2::{Digest as _, Sha256};
use std::{
    fs,
    os::unix::{
        fs::{PermissionsExt, chown},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
fn store(path: &Path, data: &[u8]) {
    fs::write(path, data).unwrap();
    chown(path, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}
fn id(n: u32) -> String {
    format!("aaaaaaaa-aaaa-4aaa-8aaa-{n:012}")
}
fn budget(profile: ExecutionProfile) -> v1::RunLimits {
    let mut b = ResourceLimits::for_profile(profile);
    if profile.prepares_program() {
        b.cpu_time_us = 30_000_000;
        b.wall_time_us = 60_000_000;
    } else if matches!(
        profile,
        ExecutionProfile::JavaRun | ExecutionProfile::PythonRun
    ) {
        b.cpu_time_us = 5_000_000;
        b.wall_time_us = 10_000_000;
    }
    serde_json::from_value(serde_json::to_value(b).unwrap()).unwrap()
}
struct Fixture {
    root: support::Root,
    dir: PathBuf,
    process: Child,
    next: u32,
}
impl Fixture {
    fn new() -> Self {
        support::require_vm();
        let mut root = support::Root::new();
        root.config.max_concurrent_runs = 1;
        root.config.identities.count = 1;
        root.config.aggregate.task_cpu_quota_us = 100000;
        root.config.per_task_ceiling.cpu_time_us = 30_000_000;
        root.config.per_task_ceiling.wall_time_us = 60_000_000;
        let mut capabilities = Vec::new();
        for (name, kind, languages) in [
            (
                "python313",
                ImageKind::LinuxCPython313X86_64V1,
                vec![v2::Language::Python313],
            ),
            (
                "java21",
                ImageKind::LinuxOpenJdk21X86_64V1,
                vec![v2::Language::Java21],
            ),
            (
                "gnu-v2",
                ImageKind::LinuxGnuCxxX86_64V1,
                vec![v2::Language::C17, v2::Language::Cpp20],
            ),
        ] {
            let image_root = format!("/var/lib/kf-judge/images/{name}");
            let raw = fs::read(format!("{image_root}/manifest.json")).unwrap();
            let c: ImageCatalog = serde_json::from_slice(&raw).unwrap();
            let toolchain = v2::RuntimeIdentity {
                toolchain_id: c.toolchain_id,
                image_kind: c.image_kind,
                image_revision: c.image_revision,
                image_digest: catalog_digest(&raw),
                runtime_version: c.runtime_version,
            };
            for language in languages {
                let (prepare, run) = match language {
                    v2::Language::C17 => (ExecutionProfile::CompileC, ExecutionProfile::Native),
                    v2::Language::Cpp20 => (ExecutionProfile::CompileCpp, ExecutionProfile::Native),
                    v2::Language::Python313 => {
                        (ExecutionProfile::PythonCheck, ExecutionProfile::PythonRun)
                    }
                    v2::Language::Java21 => {
                        (ExecutionProfile::JavaCompile, ExecutionProfile::JavaRun)
                    }
                };
                capabilities.push(v2::RuntimeCapability {
                    language,
                    toolchain: toolchain.clone(),
                    compile_ceiling: budget(prepare),
                    run_ceiling: budget(run),
                });
            }
            root.config.runtime_images.push(RegisteredImage {
                image_root,
                image_kind: kind,
                runtime_identity: toolchain,
                per_task_ceiling: root.config.per_task_ceiling.clone(),
            });
        }
        root.config.clone().validate().unwrap();
        let dir = PathBuf::from(format!("/kf/managed-pipeline-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o711)).unwrap();
        for name in ["artifacts", "staging"] {
            let p = dir.join(name);
            fs::create_dir(&p).unwrap();
            chown(&p, Some(1000), Some(1000)).unwrap();
            fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let admission = v2::AdmissionPolicy {
            schema_version: v2::SchemaVersion,
            max_concurrent_jobs: 1,
            prefetch: 1,
            max_retries: 2,
            max_job_wall_time_us: 180_000_000,
            max_package_bytes: v1::MAX_PACKAGE_BYTES,
            capabilities,
        };
        admission.validate().unwrap();
        store(
            &dir.join("admission.json"),
            &serde_json::to_vec(&admission).unwrap(),
        );
        fs::write(
            dir.join("root.json"),
            serde_json::to_vec(&root.config).unwrap(),
        )
        .unwrap();
        fs::set_permissions(dir.join("root.json"), fs::Permissions::from_mode(0o600)).unwrap();
        let mut process = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
            .env_clear()
            .args(["factory", "--config"])
            .arg(dir.join("root.json"))
            .args(["--lifetime-seconds", "1200"])
            .stdout(Stdio::null())
            .stderr(fs::File::create(dir.join("root-diagnostics.log")).unwrap())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(90);
        while !fs::metadata("/var/lib/kf-judge/factory/control.sock")
            .is_ok_and(|m| m.permissions().mode() & 0o777 == 0o660)
        {
            assert!(
                process.try_wait().unwrap().is_none(),
                "factory startup refused"
            );
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
        Self {
            root,
            dir,
            process,
            next: 1000,
        }
    }
    fn artifact(&self, data: &[u8]) -> v1::ArtifactReference {
        let digest: v1::Digest = format!("{:x}", Sha256::digest(data)).try_into().unwrap();
        store(&self.dir.join("artifacts").join(digest.as_str()), data);
        v1::ArtifactReference {
            digest,
            bytes: data.len() as u64,
        }
    }
    fn execute(
        &mut self,
        language: v2::Language,
        source: &[u8],
        points: &[(&[u8], &[u8])],
        verdict: v1::Verdict,
        legacy: bool,
    ) -> serde_json::Value {
        self.next += 10;
        let mut m: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/contracts-v2/judge-manifest.valid-python313.json"
        ))
        .unwrap();
        let n = self.next;
        let binding = v1::LeaseBinding {
            evaluation_id: id(n).try_into().unwrap(),
            attempt_id: id(n + 1).try_into().unwrap(),
            worker_id: id(8).try_into().unwrap(),
            fencing_version: 1,
        };
        let admission: v2::AdmissionPolicy =
            serde_json::from_slice(&fs::read(self.dir.join("admission.json")).unwrap()).unwrap();
        let cap = admission
            .capabilities
            .iter()
            .find(|c| c.language == language)
            .unwrap();
        m["evaluation_id"] = serde_json::json!(id(n));
        m["submission_id"] = serde_json::json!(id(n + 2));
        m["source"] = serde_json::to_value(self.artifact(source)).unwrap();
        m["language"] = serde_json::to_value(language).unwrap();
        m["toolchain"] = serde_json::to_value(&cap.toolchain).unwrap();
        m["compile_limits"] = serde_json::to_value(&cap.compile_ceiling).unwrap();
        m["run_limits"] = serde_json::to_value(&cap.run_ceiling).unwrap();
        m["job_wall_time_us"] = serde_json::json!(180_000_000);
        m["cases"] = serde_json::json!(
            points
                .iter()
                .enumerate()
                .map(|(i, (input, expected))| v1::TestCase {
                    case_id: id(n + 3 + i as u32).try_into().unwrap(),
                    input: self.artifact(input),
                    expected: self.artifact(expected)
                })
                .collect::<Vec<_>>()
        );
        let canonical = if legacy {
            m["schema_version"] = serde_json::json!(1);
            m["toolchain"] = serde_json::json!({"toolchain_id":"gnu-cxx-v1","image_kind":"linux_gnu_cxx_x86_64_v1","image_revision":1});
            v1::manifest_bytes(&serde_json::from_value(m.clone()).unwrap()).unwrap()
        } else {
            v2::manifest_bytes(&serde_json::from_value(m.clone()).unwrap()).unwrap()
        };
        let digest = format!("{:x}", Sha256::digest(canonical));
        let mut job = serde_json::json!({"manifest":m,"binding":binding,"manifest_digest":digest});
        if !legacy {
            job["attempt_number"] = serde_json::json!(1);
        }
        store(
            &self.dir.join("job.json"),
            &serde_json::to_vec(&job).unwrap(),
        );
        let mut command = Command::new(std::env::var_os("KF_WORKER_TEST_BIN").unwrap());
        command
            .env_clear()
            .uid(1000)
            .gid(1000)
            .arg(if legacy {
                "judge-submission"
            } else {
                "judge-submission-v2"
            })
            .args(["--socket", "/var/lib/kf-judge/factory/control.sock"])
            .arg("--job")
            .arg(self.dir.join("job.json"))
            .arg("--artifacts")
            .arg(self.dir.join("artifacts"))
            .arg("--staging")
            .arg(self.dir.join("staging"));
        if !legacy {
            command
                .arg("--admission")
                .arg(self.dir.join("admission.json"));
        }
        let output = command.output().unwrap();
        if !output.stderr.is_empty() {
            println!(
                "KF_PIPELINE_WORKER_DIAGNOSTIC {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        assert!(
            output.status.success(),
            "worker failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let actual: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        if actual["result"]["report"]["verdict"] != serde_json::to_value(verdict).unwrap() {
            use std::io::Read;
            let mut diagnostic = Vec::new();
            fs::File::open(self.dir.join("root-diagnostics.log"))
                .unwrap()
                .take(16_384)
                .read_to_end(&mut diagnostic)
                .unwrap();
            println!(
                "KF_PIPELINE_ROOT_DIAGNOSTIC {}",
                String::from_utf8_lossy(&diagnostic)
            );
        }
        assert_eq!(
            actual["result"]["report"]["verdict"],
            serde_json::to_value(verdict).unwrap(),
            "{actual}"
        );
        assert_eq!(
            actual["result"]["report"]["cleanup_complete"], true,
            "{actual}"
        );
        assert_eq!(
            actual["result"]["report"]["schema_version"],
            if legacy { 1 } else { 2 }
        );
        let stages = actual["stages"].as_array().unwrap();
        assert!(!stages.is_empty());
        let mut ids = std::collections::HashSet::new();
        for s in stages {
            assert!(s["helper_pid"].as_u64().unwrap() > 1);
            assert!(ids.insert(s["helper_pid"].as_u64().unwrap()));
            assert_eq!(s["report"]["errors"], serde_json::json!([]));
            assert_eq!(s["report"]["cleanup"], "complete");
        }
        assert_eq!(
            stages.len(),
            if verdict == v1::Verdict::CompilationError {
                1
            } else {
                points.len() + 1
            }
        );
        assert_eq!(fs::read_dir(self.dir.join("staging")).unwrap().count(), 0);
        let stopped = Command::new(std::env::current_exe().unwrap())
            .env_clear()
            .uid(1000)
            .gid(1000)
            .args([
                "--ignored",
                "--exact",
                "ordinary_pipeline_stopped_client",
                "--test-threads=1",
                "--nocapture",
            ])
            .output()
            .unwrap();
        assert!(stopped.status.success(), "{stopped:?}");
        assert!(String::from_utf8_lossy(&stopped.stdout).contains("KF_PIPELINE_STOPPED=1"));
        for name in ["tasks", "captures", "identities"] {
            assert_eq!(
                fs::read_dir(format!("/var/lib/kf-judge/{name}"))
                    .unwrap()
                    .count(),
                0
            );
        }
        println!(
            "KF_MANAGED_PIPELINE_PROOF {}",
            serde_json::json!({"worker_uid":1000,"language":language,"legacy":legacy,"job":job,"actual":actual,"staging_empty":true,"global_stopped":true})
        );
        actual
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = self.process.kill();
        let _ = self.process.wait();
        for name in ["requests", "socket.json", "control.sock", "lock"] {
            let _ = fs::remove_file(Path::new("/var/lib/kf-judge/factory").join(name));
        }
        let _ = fs::remove_dir("/var/lib/kf-judge/factory");
        let _ = fs::remove_dir_all(&self.dir);
    }
}
#[test]
#[ignore = "ordinary peer subprocess inside the isolated pipeline VM"]
fn ordinary_pipeline_stopped_client() {
    assert_eq!(rustix::process::geteuid().as_raw(), 1000);
    assert_eq!(rustix::process::getegid().as_raw(), 1000);
    assert!(
        FactoryClient::new(PathBuf::from("/var/lib/kf-judge/factory/control.sock"))
            .stopped()
            .unwrap()
    );
    println!("KF_PIPELINE_STOPPED=1");
}
#[test]
#[ignore = "actual ordinary Worker typed Python/Java and old/new GNU only inside capped no-NIC/no-share VM"]
fn ordinary_worker_prepares_once_and_runs_all_four_runtimes() {
    let mut f = Fixture::new();
    let points: &[(&[u8], &[u8])] = &[
        (b"", b""),
        (b"\0\xff\n", b"\0\xff\n"),
        ("你好\n".as_bytes(), "你好\n".as_bytes()),
    ];
    let py = br#"import os,sys
assert not os.path.exists('previous')
assert not os.path.exists('expected')
assert not os.path.exists('/kf')
open('previous','w').close()
sys.stdout.buffer.write(sys.stdin.buffer.read())
"#;
    f.execute(
        v2::Language::Python313,
        py,
        points,
        v1::Verdict::Accepted,
        false,
    );
    f.execute(
        v2::Language::Python313,
        py,
        &[(b"x", b"y"), (b"", b"")],
        v1::Verdict::WrongAnswer,
        false,
    );
    f.execute(
        v2::Language::Python313,
        b"def bad(:\n",
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    f.execute(
        v2::Language::Python313,
        b"raise MemoryError('fake kernel OOM')\n",
        &[(b"", b"")],
        v1::Verdict::RuntimeError,
        false,
    );
    let pyloop = f.execute(
        v2::Language::Python313,
        b"while True: pass\n",
        &[(b"", b"")],
        v1::Verdict::TimeLimitExceeded,
        false,
    );
    assert_eq!(
        pyloop["result"]["report"]["compilation"]["status"],
        "succeeded"
    );
    let java=br#"import java.io.*; public class Main {static int counter=0;static class Inner {} public static void main(String[] args) throws Exception {new Runnable(){public void run(){}}.run();if(++counter!=1||new File("previous").exists()||new File("expected").exists()||new File("/kf").exists()||Main.class.getResource("Main.java")!=null)throw new AssertionError();new File("previous").createNewFile();Helper.copy();}} class Helper {static void copy() throws Exception {System.in.transferTo(System.out);}}"#;
    let j = f.execute(
        v2::Language::Java21,
        java,
        points,
        v1::Verdict::Accepted,
        false,
    );
    assert_eq!(
        j["result"]["report"]["compilation"]["program"]["files"]
            .as_array()
            .unwrap()
            .len(),
        4
    );
    f.execute(
        v2::Language::Java21,
        java,
        &[(b"x", b"y")],
        v1::Verdict::WrongAnswer,
        false,
    );
    for source in [
        &b"public class Main { syntax error }"[..],
        &b"public class Main {static {if(System.nanoTime()>0){while(true){}}}}"[..],
        &b"package unwanted; public class Main {public static void main(String[] a){}}"[..],
        &b"public class Wrong {public static void main(String[] a){}}"[..],
    ] {
        f.execute(
            v2::Language::Java21,
            source,
            &[(b"", b"")],
            v1::Verdict::CompilationError,
            false,
        );
    }
    f.execute(v2::Language::Java21,b"public class Main {public static void main(String[] a){throw new OutOfMemoryError(\"fake kernel OOM\");}}",&[(b"",b"")],v1::Verdict::RuntimeError,false);
    gnu_points(&mut f, points);
    assert!(
        !fs::read_dir(&f.root.path)
            .unwrap()
            .any(|e| e.unwrap().file_type().unwrap().is_dir())
    );
    println!("KF_MANAGED_PIPELINE_STATUS=0");
}

fn gnu_points(f: &mut Fixture, points: &[(&[u8], &[u8])]) {
    let c=b"#include <stdio.h>\nint main(void){int c;while((c=getchar())!=EOF)putchar(c);return 0;}\n";
    let cpp = b"#include <iostream>\nint main(){char c;while(std::cin.get(c))std::cout.put(c);}\n";
    f.execute(v2::Language::C17, c, points, v1::Verdict::Accepted, false);
    f.execute(
        v2::Language::Cpp20,
        cpp,
        points,
        v1::Verdict::Accepted,
        false,
    );
    f.execute(v2::Language::C17, c, points, v1::Verdict::Accepted, true);
    f.execute(
        v2::Language::Cpp20,
        cpp,
        points,
        v1::Verdict::Accepted,
        true,
    );
}
#[test]
#[ignore = "targeted actual ordinary Worker new/legacy GNU after typed staging changes inside capped VM"]
fn ordinary_worker_runs_versioned_and_legacy_gnu() {
    let mut f = Fixture::new();
    let points: &[(&[u8], &[u8])] = &[
        (b"", b""),
        (b"\0\xff\n", b"\0\xff\n"),
        ("你好\n".as_bytes(), "你好\n".as_bytes()),
    ];
    gnu_points(&mut f, points);
    assert!(
        !fs::read_dir(&f.root.path)
            .unwrap()
            .any(|e| e.unwrap().file_type().unwrap().is_dir())
    );
    println!("KF_MANAGED_GNU_PIPELINE_STATUS=0");
}
