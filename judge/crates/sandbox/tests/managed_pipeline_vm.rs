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
    if profile == ExecutionProfile::JavaCompile {
        // Actual javac of the supported 256-class boundary under single-vCPU
        // TCG needs a finite larger fixture budget. Production limits stay fixed.
        b.cpu_time_us = 60_000_000;
        b.wall_time_us = 120_000_000;
    } else if profile.prepares_program() {
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
        root.config.per_task_ceiling.cpu_time_us = 60_000_000;
        root.config.per_task_ceiling.wall_time_us = 120_000_000;
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
        self.execute_with(language, source, points, verdict, legacy, |_| {})
    }
    fn make_job(
        &mut self,
        language: v2::Language,
        source: &[u8],
        points: &[(&[u8], &[u8])],
        legacy: bool,
        edit: impl FnOnce(&mut serde_json::Value),
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
        edit(&mut m);
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
        job
    }
    fn worker_command(&self, legacy: bool) -> Command {
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
        command
    }
    fn assert_drained(&self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
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
            if stopped.status.success()
                && String::from_utf8_lossy(&stopped.stdout).contains("KF_PIPELINE_STOPPED=1")
            {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "global drain refused: {stopped:?}"
            );
            std::thread::sleep(Duration::from_millis(25));
        }
        for name in ["tasks", "captures", "identities"] {
            assert_eq!(
                fs::read_dir(format!("/var/lib/kf-judge/{name}"))
                    .unwrap()
                    .count(),
                0
            );
        }
    }
    fn execute_with(
        &mut self,
        language: v2::Language,
        source: &[u8],
        points: &[(&[u8], &[u8])],
        verdict: v1::Verdict,
        legacy: bool,
        edit: impl FnOnce(&mut serde_json::Value),
    ) -> serde_json::Value {
        let job = self.make_job(language, source, points, legacy, edit);
        let output = self.worker_command(legacy).output().unwrap();
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
        assert!(
            !stages.is_empty()
                || (verdict == v1::Verdict::SystemError
                    && actual["result"]["report"]["failure_code"] == "artifact_unavailable")
        );
        let mut ids = std::collections::HashSet::new();
        for s in stages {
            assert!(s["helper_pid"].as_u64().unwrap() > 1);
            assert!(ids.insert(s["helper_pid"].as_u64().unwrap()));
            assert_eq!(s["report"]["errors"], serde_json::json!([]));
            assert_eq!(s["report"]["cleanup"], "complete");
        }
        if verdict == v1::Verdict::SystemError {
            assert!(stages.len() <= points.len() + 1);
        } else {
            assert_eq!(
                stages.len(),
                if verdict == v1::Verdict::CompilationError {
                    1
                } else {
                    points.len() + 1
                }
            );
        }
        assert_eq!(fs::read_dir(self.dir.join("staging")).unwrap().count(), 0);
        self.assert_drained();
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

#[test]
#[ignore = "actual ordinary Worker language/encoding/class boundary and artifact failures only in capped VM"]
fn ordinary_worker_encoding_and_class_bounds() {
    let mut f = Fixture::new();
    let mut expected = "café".as_bytes().to_vec();
    expected.extend_from_slice(b"\0\xff\n");
    f.execute(
        v2::Language::Python313,
        b"# coding: latin-1\nimport os,sys\nassert not os.path.exists('old')\nopen('old','w').close()\nsys.stdout.buffer.write('caf\xe9'.encode('utf-8')+sys.stdin.buffer.read())\n",
        &[(b"", "café".as_bytes()), (b"\0\xff\n", &expected)],
        v1::Verdict::Accepted,
        false,
    );
    let java = r#"import java.io.*; public class Main extends Entry {} class Entry {static int count;public static void main(String[] a)throws Exception{if(++count!=1||new File("old").exists())throw new AssertionError();new File("old").createNewFile();System.out.print("\0你好");System.in.transferTo(System.out);}}"#;
    let mut expected = "\0你好".as_bytes().to_vec();
    expected.extend_from_slice(b"\0\xff\n");
    let inherited = f.execute(
        v2::Language::Java21,
        java.as_bytes(),
        &[(b"", "\0你好".as_bytes()), (b"\0\xff\n", &expected)],
        v1::Verdict::Accepted,
        false,
    );
    assert_eq!(
        inherited["result"]["report"]["compilation"]["program"]["files"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    let mut classes = String::from(
        "public class Main {static int n;public static void main(String[] a){if(++n!=1)throw new AssertionError();System.out.print(A254.value());}}\n",
    );
    for i in 0..255 {
        classes.push_str(&format!(
            "class A{i:03} {{static String value(){{return \"last\";}}}}\n"
        ));
    }
    let boundary = f.execute(
        v2::Language::Java21,
        classes.as_bytes(),
        &[(b"", b"last"), (b"x", b"last")],
        v1::Verdict::Accepted,
        false,
    );
    assert_eq!(
        boundary["result"]["report"]["compilation"]["program"]["files"]
            .as_array()
            .unwrap()
            .len(),
        256
    );
    classes.push_str("class Extra {}\n");
    let oversized = f.execute(
        v2::Language::Java21,
        classes.as_bytes(),
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    assert!(oversized["result"]["report"]["compilation"]["program"].is_null());
    assert!(
        oversized["result"]["report"]["compilation"]["diagnostics"]
            .as_str()
            .unwrap()
            .contains("256")
    );
    f.execute(
        v2::Language::Java21,
        r#"public class Main {public static void main(String[] a){}} class Aé {}"#.as_bytes(),
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    let long_name = format!(
        "public class Main {{public static void main(String[] a){{}}}} class A{} {{}}",
        "x".repeat(123)
    );
    f.execute(
        v2::Language::Java21,
        long_name.as_bytes(),
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    f.execute(
        v2::Language::Python313,
        b"# coding: unknown_codec\npass\n",
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    println!("KF_MANAGED_ENCODING_CLASS_BOUNDS_STATUS=0");
}

#[test]
#[ignore = "focused current-binary ordinary managed WA, syntax and entry errors in capped VM"]
fn ordinary_worker_rechecks_managed_grades_and_entry_errors() {
    let mut f = Fixture::new();
    let points: &[(&[u8], &[u8])] = &[(b"", b""), (b"\0\xff\n", b"wrong")];
    for (language, source) in [
        (v2::Language::Python313, &b"import os,sys\nassert not os.path.exists('previous')\nassert not os.path.exists('expected')\nassert not os.path.exists('/kf')\nopen('previous','w').close()\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"[..]),
        (v2::Language::Java21, &b"import java.io.*; public class Main {static int n;public static void main(String[] a)throws Exception{if(++n!=1||new File(\"previous\").exists()||new File(\"expected\").exists()||new File(\"/kf\").exists()||Main.class.getResource(\"Main.java\")!=null)throw new AssertionError();new File(\"previous\").createNewFile();System.in.transferTo(System.out);}}"[..]),
    ] {
        let judged=f.execute(language,source,points,v1::Verdict::WrongAnswer,false);
        assert_eq!(judged["result"]["report"]["cases"][0]["verdict"],"accepted");
        assert_eq!(judged["result"]["report"]["cases"][1]["verdict"],"wrong_answer");
    }
    f.execute(
        v2::Language::Python313,
        b"open('top-level','w').close()\ndef invalid(:\n",
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    let missing = f.execute(
        v2::Language::Java21,
        b"public class Main {static {if(System.nanoTime()>0){while(true){}}}}",
        &[(b"", b"")],
        v1::Verdict::CompilationError,
        false,
    );
    assert_eq!(missing["stages"].as_array().unwrap().len(), 1);
    assert!(
        missing["result"]["report"]["compilation"]["diagnostics"]
            .as_str()
            .unwrap()
            .contains("Main")
    );
    assert!(missing["result"]["report"]["compilation"]["program"].is_null());
    println!("KF_MANAGED_CURRENT_GRADES_STATUS=0");
}

#[test]
#[ignore = "actual ordinary Worker preparation limits and artifact failures only in capped VM"]
fn ordinary_worker_preparation_limits_and_artifact_failures() {
    let mut f = Fixture::new();
    for (language, source, cpu) in [
        (v2::Language::Python313, b"x=0\n".repeat(60_000), 500_000),
        (
            v2::Language::Java21,
            b"public class Main {public static void main(String[] a){}}".to_vec(),
            1_000_000,
        ),
    ] {
        let limited = f.execute_with(
            language,
            &source,
            &[(b"", b"")],
            v1::Verdict::CompilationError,
            false,
            |m| {
                m["compile_limits"]["cpu_time_us"] = serde_json::json!(cpu);
            },
        );
        assert_eq!(
            limited["result"]["report"]["compilation"]["status"],
            "compilation_limit"
        );
        assert!(limited["result"]["report"]["compilation"]["program"].is_null());
        assert!(
            limited["result"]["case_run_ids"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }
    // Manifest identities/digests remain valid. Corrupt only actual fixture bytes
    // to prove the ordinary Worker fails closed at each read boundary.
    for language in [v2::Language::Python313, v2::Language::Java21] {
        let source: &[u8] = if language == v2::Language::Python313 {
            b"import sys\nsys.stdout.buffer.write(b'answer')\n"
        } else {
            b"public class Main {public static void main(String[] a){System.out.print(\"answer\");}}"
        };
        for (field, index, stage_count) in [
            ("source", None, 0),
            ("input", Some(0), 1),
            ("expected", Some(0), 2),
        ] {
            let artifacts = f.dir.join("artifacts");
            let actual = f.execute_with(
                language,
                source,
                &[(b"input", b"answer")],
                v1::Verdict::SystemError,
                false,
                |m| {
                    let a = match index {
                        None => &m[field],
                        Some(i) => &m["cases"][i][field],
                    };
                    let path = artifacts.join(a["digest"].as_str().unwrap());
                    let mut bytes = fs::read(&path).unwrap();
                    bytes[0] ^= 1;
                    store(&path, &bytes);
                },
            );
            assert_eq!(actual["stages"].as_array().unwrap().len(), stage_count);
            assert_eq!(
                actual["result"]["report"]["failure_code"],
                "artifact_unavailable"
            );
            if stage_count < 2 {
                assert!(actual["result"]["report"]["run_cpu_time_us"].is_null());
            }
        }
    }
    // A corrupted read must not poison the next accepted job or its slot.
    f.execute(
        v2::Language::Python313,
        b"print('fresh')\n",
        &[(b"", b"fresh\n")],
        v1::Verdict::Accepted,
        false,
    );
    f.execute(
        v2::Language::Java21,
        b"public class Main {public static void main(String[] a){System.out.print(\"fresh\");}}",
        &[(b"", b"fresh")],
        v1::Verdict::Accepted,
        false,
    );
    println!("KF_MANAGED_LANGUAGE_BOUNDS_STATUS=0");
}

#[test]
#[ignore = "actual ordinary Worker kernel limits and spoofed language errors only in capped VM"]
fn ordinary_worker_trusted_limits_and_spoofed_oom() {
    let mut f = Fixture::new();
    for (source, verdict) in [
        (
            &b"raise MemoryError('fake kernel OOM')\n"[..],
            v1::Verdict::RuntimeError,
        ),
        (&b"while True: pass\n"[..], v1::Verdict::TimeLimitExceeded),
        (
            &b"import os\nx=b'x'*65536\nwhile True: os.write(1,x)\n"[..],
            v1::Verdict::OutputLimitExceeded,
        ),
        (
            &b"blocks=[]\nwhile True: blocks.append(bytearray(8*1024*1024))\n"[..],
            v1::Verdict::MemoryLimitExceeded,
        ),
    ] {
        let result = f.execute(
            v2::Language::Python313,
            source,
            &[(b"", b"")],
            verdict,
            false,
        );
        assert_eq!(
            result["result"]["report"]["compilation"]["status"],
            "succeeded"
        );
    }
    for (source,verdict) in [
        (&b"public class Main {public static void main(String[] a){throw new OutOfMemoryError(\"fake kernel OOM\");}}"[..],v1::Verdict::RuntimeError),
        (&b"public class Main {public static void main(String[] a){while(true){}}}"[..],v1::Verdict::TimeLimitExceeded),
        (&b"public class Main {public static void main(String[] a)throws Exception{byte[] b=new byte[65536];while(true)System.out.write(b);}}"[..],v1::Verdict::OutputLimitExceeded),
        (&b"import sun.misc.Unsafe; import java.lang.reflect.*; public class Main {public static void main(String[] a)throws Exception{Field f=Unsafe.class.getDeclaredField(\"theUnsafe\");f.setAccessible(true);Unsafe u=(Unsafe)f.get(null);while(true){long p=u.allocateMemory(16777216);u.setMemory(p,16777216,(byte)1);}}}"[..],v1::Verdict::MemoryLimitExceeded),
    ] {let result=f.execute(v2::Language::Java21,source,&[(b"",b"")],verdict,false);assert_eq!(result["result"]["report"]["compilation"]["status"],"succeeded");}
    f.execute(
        v2::Language::Python313,
        b"print('after-limits')\n",
        &[(b"", b"after-limits\n")],
        v1::Verdict::Accepted,
        false,
    );
    f.execute(v2::Language::Java21,b"public class Main {public static void main(String[] a){System.out.print(\"after-limits\");}}",&[(b"",b"after-limits")],v1::Verdict::Accepted,false);
    println!("KF_MANAGED_WORKER_LIMITS_STATUS=0");
}

#[test]
#[ignore = "actual Worker death with live Python fork/JVM threads and proven publication recovery only in capped VM"]
fn ordinary_worker_death_drains_tree_and_recovers_publication() {
    let mut f = Fixture::new();
    for language in [v2::Language::Python313, v2::Language::Java21] {
        let source: &[u8] = if language == v2::Language::Python313 {
            b"import os,time\np=os.fork()\nif p==0:\n while True: time.sleep(1)\nopen('started','w').write('python-fork-live')\nwhile True: time.sleep(1)\n"
        } else {
            b"import java.nio.file.*; public class Main {public static void main(String[] a)throws Exception{for(int i=0;i<3;i++)new Thread(()->{while(true)try{Thread.sleep(1000);}catch(InterruptedException e){}}).start();Files.writeString(Path.of(\"started\"),\"java-threads-live\");while(true)Thread.sleep(1000);}}"
        };
        let job = f.make_job(language, source, &[(b"", b"")], false, |_| {});
        let run_id = format!("r-{}-0", job["binding"]["attempt_id"].as_str().unwrap());
        let group = f.root.path.join(format!("kf-{run_id}"));
        let mut worker = f
            .worker_command(false)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let worker_pid = worker.id();
        let deadline = Instant::now() + Duration::from_secs(100);
        let (members, marker, threads) = loop {
            if let Some(status) = worker.try_wait().unwrap() {
                let output = worker.wait_with_output().unwrap();
                panic!(
                    "worker ended before live marker ({status}): {} {}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                );
            }
            let members: Vec<u32> = fs::read_to_string(group.join("cgroup.procs"))
                .unwrap_or_default()
                .split_whitespace()
                .map(|p| p.parse().unwrap())
                .collect();
            if let Some((marker, threads)) = members.iter().find_map(|pid| {
                let executable = fs::read_link(format!("/proc/{pid}/exe")).ok()?;
                let expected = if language == v2::Language::Python313 {
                    "python3.13"
                } else {
                    "java"
                };
                if executable.file_name()?.to_str()? != expected {
                    return None;
                }
                let path = PathBuf::from(format!("/proc/{pid}/root/work/started"));
                let marker = fs::read_to_string(path).ok()?;
                let threads = fs::read_dir(format!("/proc/{pid}/task")).ok()?.count();
                let live = if language == v2::Language::Python313 {
                    members.len() >= 2 && marker == "python-fork-live"
                } else {
                    threads >= 4 && marker == "java-threads-live"
                };
                live.then_some((marker, threads))
            }) {
                break (members, marker, threads);
            }
            assert!(
                Instant::now() < deadline,
                "actual user tree not observed in {group:?}"
            );
            std::thread::sleep(Duration::from_millis(10));
        };
        assert!(
            fs::read_dir(f.dir.join("staging")).unwrap().count() > 0,
            "live ordinary publication required"
        );
        worker.kill().unwrap();
        let killed = worker.wait_with_output().unwrap();
        assert!(!killed.status.success());
        f.assert_drained();
        assert!(!group.exists());
        for pid in &members {
            assert!(
                !PathBuf::from(format!("/proc/{pid}")).exists(),
                "descendant {pid} remains"
            );
        }
        assert!(
            fs::read_dir(f.dir.join("staging")).unwrap().count() > 0,
            "dead publication must remain until authenticated ordinary recovery"
        );
        println!(
            "KF_MANAGED_WORKER_DEATH_PROOF {}",
            serde_json::json!({"job":job,"language":language,"worker_uid":1000,"worker_pid":worker_pid,"observed_target_pids":members,"observed_threads":threads,"actual_marker":marker,"worker_exit":killed.status.to_string(),"global_stopped":true,"root_owned_zero":true,"dead_publication_present":true})
        );
        // A fresh ordinary Worker recovers the dead owner's journal, then proves
        // its own prepare/run and exact bytes without inheriting the killed tree.
        let (fresh, expected): (&[u8], &[u8]) = if language == v2::Language::Python313 {
            (
                b"import os\nassert not os.path.exists('started')\nprint('recovered')\n",
                b"recovered\n",
            )
        } else {
            (b"import java.io.*; public class Main {public static void main(String[] a){if(new File(\"started\").exists())throw new AssertionError();System.out.print(\"recovered\");}}",b"recovered")
        };
        f.execute(
            language,
            fresh,
            &[(b"", expected)],
            v1::Verdict::Accepted,
            false,
        );
    }
    println!("KF_MANAGED_WORKER_DEATH_STATUS=0");
}

#[test]
#[ignore = "actual ordinary Worker production adapter cancellation during check/javac and live trees in capped VM"]
fn ordinary_worker_control_cancels_preparation_and_live_runs() {
    let mut f = Fixture::new();
    for language in [v2::Language::Python313, v2::Language::Java21] {
        for preparing in [true, false] {
            let source = if preparing {
                if language == v2::Language::Python313 {
                    b"x=0\n".repeat(60_000)
                } else {
                    let mut s =
                        String::from("public class Main {public static void main(String[] a){}}\n");
                    for i in 0..1200 {
                        s.push_str(&format!("class Many{i} {{int i{i}=1;}}\n"));
                    }
                    s.into_bytes()
                }
            } else if language == v2::Language::Python313 {
                b"import os,time\np=os.fork()\nif p==0:\n while True: time.sleep(1)\nopen('started','w').write('python-fork-live')\nwhile True: time.sleep(1)\n".to_vec()
            } else {
                b"import java.nio.file.*; public class Main {public static void main(String[] a)throws Exception{for(int i=0;i<3;i++)new Thread(()->{while(true)try{Thread.sleep(1000);}catch(InterruptedException e){}}).start();Files.writeString(Path.of(\"started\"),\"java-threads-live\");while(true)Thread.sleep(1000);}}".to_vec()
            };
            let job = f.make_job(
                language,
                &source,
                &[(b"", b""), (b"next", b"next")],
                false,
                |_| {},
            );
            let stage_id = if preparing {
                format!("c-{}", job["binding"]["attempt_id"].as_str().unwrap())
            } else {
                format!("r-{}-0", job["binding"]["attempt_id"].as_str().unwrap())
            };
            let group = f.root.path.join(format!("kf-{stage_id}"));
            let mut worker = Command::new(std::env::var_os("KF_WORKER_CONTROL_TEST_BIN").unwrap())
                .env_clear()
                .uid(1000)
                .gid(1000)
                .env("KF_SANDBOX_INDEPENDENT_VM", "1")
                .env("KF_WORKER_CONTROL_DIR", &f.dir)
                .args([
                    "--ignored",
                    "--exact",
                    "runtime_cancel_from_trusted_control",
                    "--test-threads=1",
                    "--nocapture",
                ])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap();
            let worker_pid = worker.id();
            let deadline = Instant::now() + Duration::from_secs(100);
            let (members, observed) = loop {
                assert!(
                    worker.try_wait().unwrap().is_none(),
                    "worker ended before observed cancellable stage"
                );
                let members: Vec<u32> = fs::read_to_string(group.join("cgroup.procs"))
                    .unwrap_or_default()
                    .split_whitespace()
                    .map(|p| p.parse().unwrap())
                    .collect();
                let observed = members.iter().find_map(|pid| {
                    if preparing {
                        let exe = fs::read_link(format!("/proc/{pid}/exe")).ok()?;
                        let expected = if language == v2::Language::Python313 {
                            "python3.13"
                        } else {
                            "javac"
                        };
                        if exe.file_name()?.to_str()? != expected {
                            return None;
                        }
                        let cmd = fs::read(format!("/proc/{pid}/cmdline")).ok()?;
                        let required = if language == v2::Language::Python313 {
                            "/usr/lib/kf/python_check.py"
                        } else {
                            "/work/Main.java"
                        };
                        if !cmd.split(|b| *b == 0).any(|a| a == required.as_bytes()) {
                            return None;
                        }
                        Some(format!("isolated-{expected}-exec-with-fixed-source"))
                    } else {
                        let marker =
                            fs::read_to_string(format!("/proc/{pid}/root/work/started")).ok()?;
                        let threads = fs::read_dir(format!("/proc/{pid}/task")).ok()?.count();
                        if (language == v2::Language::Python313
                            && members.len() >= 2
                            && marker == "python-fork-live")
                            || (language == v2::Language::Java21
                                && threads >= 4
                                && marker == "java-threads-live")
                        {
                            Some(marker)
                        } else {
                            None
                        }
                    }
                });
                if let Some(observed) = observed {
                    break (members, observed);
                }
                assert!(
                    Instant::now() < deadline,
                    "requested actual stage was not observed"
                );
                std::thread::sleep(Duration::from_millis(5));
            };
            store(&f.dir.join("cancel"), b"cancel\n");
            let output = worker.wait_with_output().unwrap();
            fs::remove_file(f.dir.join("cancel")).unwrap();
            assert!(output.status.success(), "{output:?}");
            let text = String::from_utf8(output.stdout).unwrap();
            let proof: serde_json::Value = serde_json::from_str(
                text.lines()
                    .find_map(|l| l.split_once("KF_WORKER_CONTROL_PROOF ").map(|(_, p)| p))
                    .unwrap_or_else(|| panic!("successful control test omitted proof: {text}")),
            )
            .unwrap();
            assert_eq!(proof["result"]["report"]["verdict"], "cancelled");
            assert_eq!(proof["result"]["report"]["cleanup_complete"], true);
            assert_eq!(
                proof["stages"].as_array().unwrap().len(),
                if preparing { 1 } else { 2 }
            );
            assert_eq!(
                proof["result"]["skipped"].as_array().unwrap().len(),
                if preparing { 2 } else { 1 }
            );
            f.assert_drained();
            assert!(!group.exists());
            for pid in &members {
                assert!(!PathBuf::from(format!("/proc/{pid}")).exists());
            }
            assert_eq!(fs::read_dir(f.dir.join("staging")).unwrap().count(), 0);
            println!(
                "KF_MANAGED_WORKER_CONTROL_PROOF {}",
                serde_json::json!({"language":language,"preparing":preparing,"job":job,"worker_uid":1000,"worker_pid":worker_pid,"observed_target_pids":members,"observed":observed,"actual":proof,"staging_empty":true,"global_stopped":true})
            );
        }
        let (fresh, expected): (&[u8], &[u8]) = if language == v2::Language::Python313 {
            (b"print('after-cancel')\n", b"after-cancel\n")
        } else {
            (b"public class Main {public static void main(String[] a){System.out.print(\"after-cancel\");}}",b"after-cancel")
        };
        f.execute(
            language,
            fresh,
            &[(b"", expected)],
            v1::Verdict::Accepted,
            false,
        );
    }
    println!("KF_MANAGED_WORKER_CONTROL_STATUS=0");
}

#[test]
#[ignore = "actual ordinary Worker checked Python/compiled Java program cannot be replaced or forged in capped VM"]
fn ordinary_worker_uses_sealed_program_after_backing_mutation() {
    let mut f = Fixture::new();
    for language in [v2::Language::Python313, v2::Language::Java21] {
        let source: &[u8] = if language == v2::Language::Python313 {
            b"import os,sys\nassert not os.path.exists('old')\nopen('old','w').close()\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        } else {
            b"import java.io.*; public class Main {static int n;public static void main(String[] a)throws Exception{if(++n!=1||new File(\"old\").exists())throw new AssertionError();new File(\"old\").createNewFile();Helper.copy();}} class Helper {static void copy()throws Exception{System.in.transferTo(System.out);}}"
        };
        let job = f.make_job(
            language,
            source,
            &[(b"", b""), (b"\0\xff\n", b"\0\xff\n")],
            false,
            |_| {},
        );
        let output = Command::new(std::env::var_os("KF_WORKER_CONTROL_TEST_BIN").unwrap())
            .env_clear()
            .uid(1000)
            .gid(1000)
            .env("KF_SANDBOX_INDEPENDENT_VM", "1")
            .env("KF_WORKER_CONTROL_DIR", &f.dir)
            .args([
                "--ignored",
                "--exact",
                "runtime_checked_program_survives_backing_mutation_and_rejects_forgery",
                "--test-threads=1",
                "--nocapture",
            ])
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        let text = String::from_utf8(output.stdout).unwrap();
        let proof: serde_json::Value = serde_json::from_str(
            text.lines()
                .find_map(|l| {
                    l.split_once("KF_WORKER_SEALED_PROGRAM_PROOF ")
                        .map(|(_, p)| p)
                })
                .unwrap_or_else(|| panic!("successful sealed-program test omitted proof: {text}")),
        )
        .unwrap();
        assert_eq!(proof["rejected_metadata_mutations"], 14);
        assert_eq!(proof["backing_changed_after_real_preparation"], true);
        assert_eq!(proof["stages"].as_array().unwrap().len(), 3);
        f.assert_drained();
        assert_eq!(fs::read_dir(f.dir.join("staging")).unwrap().count(), 0);
        println!(
            "KF_MANAGED_WORKER_SEALED_PROGRAM_PROOF {}",
            serde_json::json!({"language":language,"job":job,"worker_uid":1000,"actual":proof,"staging_empty":true,"global_stopped":true})
        );
    }
    println!("KF_MANAGED_WORKER_SEALED_PROGRAM_STATUS=0");
}
