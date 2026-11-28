#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
//! Actual fixed-profile diagnostics, only in the isolated guest.
#[allow(dead_code)]
mod support;

use kf_contracts::{
    SchemaVersion,
    diagnostic::{
        BoundaryOutcome, DiagnosticBinding, DiagnosticBoundaryReport, DiagnosticMode,
        DiagnosticResult, DiagnosticRun, ResultState, RunKind, UbsanCategory,
    },
    judging::{self as v1, decode_wire},
    judging_v2 as v2,
};
use kf_sandbox::{
    image::{ImageCatalog, catalog_digest},
    spec::{ExecutionProfile, ResourceLimits},
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    os::unix::{
        fs::{MetadataExt, PermissionsExt, chown},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU32, Ordering},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const SOURCE: &[u8] = b"#include <stdio.h>\nint main(void) {\n#ifdef __OPTIMIZE__\n  puts(\"1\");\n#else\n  puts(\"2\");\n#endif\n  return 0;\n}\n";

struct Factory(Child);
impl Drop for Factory {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

struct GuestDirectory(PathBuf);
impl Drop for GuestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn reference(bytes: &[u8]) -> v1::ArtifactReference {
    v1::ArtifactReference {
        digest: format!("{:x}", Sha256::digest(bytes)).try_into().unwrap(),
        bytes: bytes.len() as u64,
    }
}

fn id() -> v1::Identifier {
    static NEXT: AtomicU32 = AtomicU32::new(1);
    let next = NEXT.fetch_add(1, Ordering::Relaxed);
    format!("aaaaaaaa-aaaa-4aaa-8aaa-{next:012}")
        .try_into()
        .unwrap()
}

fn limits(profile: ExecutionProfile, max_wall: u64) -> v1::RunLimits {
    let mut values = ResourceLimits::for_profile(profile);
    values.wall_time_us = values.wall_time_us.min(max_wall);
    serde_json::from_value(serde_json::to_value(values).unwrap()).unwrap()
}

fn private(path: &Path, bytes: &[u8], ordinary: bool) {
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    if ordinary {
        chown(path, Some(1000), Some(1000)).unwrap();
    }
}

fn ordinary() -> Command {
    let mut command = Command::new(std::env::var_os("KF_DIAGNOSTIC_WORKER_BIN").unwrap());
    command.env_clear().uid(1000).gid(1000);
    command
}

const FACTORY_SOCKET: &str = "/var/lib/kf-judge/factory/control.sock";

fn start_factory(path: &Path) -> Factory {
    let mut factory = Factory(
        Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
            .env_clear()
            .args(["factory", "--config"])
            .arg(path)
            .args(["--lifetime-seconds", "180"])
            .spawn()
            .unwrap(),
    );
    let socket = FACTORY_SOCKET;
    let ready_by = Instant::now() + Duration::from_secs(35);
    while !fs::metadata(socket).is_ok_and(|entry| entry.permissions().mode() & 0o777 == 0o660) {
        assert!(
            factory.0.try_wait().unwrap().is_none(),
            "diagnostic factory refused"
        );
        assert!(Instant::now() < ready_by);
        std::thread::sleep(Duration::from_millis(10));
    }
    factory
}

#[test]
#[ignore = "only in a capped independent VM; fixed GNU C17/C++20 O2 and O0 compile and execute"]
fn diagnostic_transport_compares_fixed_profiles_and_drains_factory() {
    transport(DiagnosticMode::Optimization, false);
}

#[test]
#[ignore = "independent VM: actual C17/C++20 UBSan diagnostic Worker and cleaned sandbox"]
fn diagnostic_transport_delivers_only_the_structured_ubsan_finding() {
    transport(DiagnosticMode::Ubsan, false);
}

#[test]
#[ignore = "independent VM: actual fixed GNU batch jobs and cleanup"]
fn diagnostic_batch_replay_uses_all_independent_cases() {
    transport(DiagnosticMode::Optimization, true);
}

fn transport(mode: DiagnosticMode, batch: bool) {
    support::require_vm();
    let mut root = support::Root::new();
    root.config.identities.count = 1;
    root.config.max_concurrent_runs = 1;
    root.config.aggregate.task_cpu_quota_us = 100_000;
    root.config.allow_diagnostic_compilation = mode == DiagnosticMode::Optimization;
    root.config.allow_ubsan_compilation = mode == DiagnosticMode::Ubsan;
    const UBSAN_SOURCE: &[u8] = br#"#ifdef __cplusplus
extern "C"
#endif
long read(int, void *, unsigned long);
int main(void) {
    char sample = '0';
    if (read(0, &sample, 1) != 1) return 2;
    volatile int limit = 2147483647;
    volatile int result = limit + (sample == '1');
    return result == 0;
}
"#;
    let (source, input, expected, wall): (&[u8], &[u8], &[u8], u64) = match mode {
        DiagnosticMode::Optimization => (SOURCE, b"", b"2\n", 55_000_000),
        DiagnosticMode::Ubsan => (UBSAN_SOURCE, b"1", b"", 30_000_000),
    };

    let raw = fs::read(Path::new(&root.config.image_root).join("manifest.json")).unwrap();
    let catalog: ImageCatalog = serde_json::from_slice(&raw).unwrap();
    let pin = v2::RuntimeIdentity {
        toolchain_id: catalog.toolchain_id,
        image_kind: catalog.image_kind,
        image_revision: catalog.image_revision,
        image_digest: catalog_digest(&raw),
        runtime_version: catalog.runtime_version,
    };
    root.config.runtime_identity = Some(pin.clone());
    root.config.clone().validate().unwrap();

    let folder = GuestDirectory(PathBuf::from(format!(
        "/kf/diagnostic-{}",
        std::process::id()
    )));
    fs::create_dir(&folder.0).unwrap();
    fs::set_permissions(&folder.0, fs::Permissions::from_mode(0o711)).unwrap();
    let stage = folder.0.join("stage");
    fs::create_dir(&stage).unwrap();
    chown(&stage, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o700)).unwrap();
    let admission_path = folder.0.join("admission.json");
    let root_path = folder.0.join("root.json");
    let config = serde_json::to_vec(&root.config).unwrap();
    let config_digest = format!("{:x}", Sha256::digest(&config));
    private(&root_path, &config, false);
    let available = [
        (v2::Language::C17, ExecutionProfile::CompileC),
        (v2::Language::Cpp20, ExecutionProfile::CompileCpp),
    ];
    let admission = v2::AdmissionPolicy {
        schema_version: v2::SchemaVersion,
        max_concurrent_jobs: 1,
        prefetch: 1,
        max_retries: 0,
        max_job_wall_time_us: 55_000_000,
        max_package_bytes: 1024 * 1024,
        capabilities: available
            .into_iter()
            .map(|(language, profile)| v2::RuntimeCapability {
                language,
                toolchain: pin.clone(),
                compile_ceiling: limits(profile, 20_000_000),
                run_ceiling: limits(ExecutionProfile::Native, 5_000_000),
            })
            .collect(),
    };
    admission.validate().unwrap();
    private(
        &admission_path,
        &serde_json::to_vec(&admission).unwrap(),
        true,
    );
    let _factory = start_factory(&root_path);
    if batch {
        replay_cases(
            &folder.0,
            &stage,
            &admission_path,
            &config_digest,
            &admission,
        );
        return;
    }
    let socket = FACTORY_SOCKET;
    for cap in &admission.capabilities {
        let binding = DiagnosticBinding {
            request_id: id(),
            execution_id: id(),
            worker_id: id(),
        };
        let run = DiagnosticRun {
            schema_version: SchemaVersion,
            kind: RunKind::Value,
            mode,
            binding: binding.clone(),
            problem_version_id: id(),
            configuration: v2::LanguageConfiguration {
                language: cap.language,
                toolchain: pin.clone(),
                compile_limits: cap.compile_ceiling.clone(),
                run_limits: cap.run_ceiling.clone(),
                job_wall_time_us: wall,
            },
            claim_expires_at_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as u64
                + 59_000,
            source: reference(source),
            sample: v1::TestCase {
                case_id: binding.execution_id,
                input: reference(input),
                expected: reference(expected),
            },
        };
        run.validate().unwrap();
        let header = serde_json::to_vec(&run).unwrap();
        let mut worker = ordinary()
            .args(["diagnose-public-sample", "--socket", socket])
            .arg("--staging")
            .arg(&stage)
            .arg("--admission")
            .arg(&admission_path)
            .args(["--worker-id", &binding.worker_id.as_uuid().to_string()])
            .args(["--factory-config-sha256", &config_digest])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut writer = worker.stdin.take().unwrap();
        writer
            .write_all(&(header.len() as u32).to_be_bytes())
            .unwrap();
        writer.write_all(&header).unwrap();
        writer.write_all(source).unwrap();
        writer.write_all(input).unwrap();
        writer.write_all(expected).unwrap();
        // The parent owns the pipe for the lifetime of the attempt.
        let output = worker.wait_with_output().unwrap();
        drop(writer);
        assert!(output.status.success());
        let report: DiagnosticResult = decode_wire(&output.stdout).unwrap();
        assert_eq!(report.binding, binding);
        assert_eq!(report.state, ResultState::Observed, "{report:?}");
        match mode {
            DiagnosticMode::Optimization => {
                let observation = report.observation.unwrap();
                assert_eq!(observation.o2, v1::Verdict::WrongAnswer);
                assert_eq!(observation.o0, Some(v1::Verdict::Accepted));
                println!("diagnostic {:?} O2=wrong_answer O0=accepted", cap.language);
            }
            DiagnosticMode::Ubsan => {
                assert!(report.observation.is_none());
                let envelope = String::from_utf8(output.stdout).unwrap();
                assert!(!envelope.contains("2147483647") && !envelope.contains("/work/"));
                let observation = report.ubsan.unwrap();
                assert_eq!(observation.verdict, v1::Verdict::RuntimeError);
                let finding = observation.finding.unwrap();
                assert_eq!(finding.category, UbsanCategory::SignedIntegerOverflow);
                assert_eq!((finding.line, finding.column), (9, 33));
                println!(
                    "KF_WORKER_UBSAN {:?} sample=1 {:?} {}:{}",
                    cap.language, finding.category, finding.line, finding.column
                );
            }
        }
        assert_eq!(fs::read_dir(&stage).unwrap().count(), 0);
    }
    let drained_by = Instant::now() + Duration::from_secs(5);
    loop {
        let probe = ordinary()
            .args(["check-stopped", "--socket", socket])
            .output()
            .unwrap();
        if probe.status.success() && probe.stdout == b"{\"cleanup_complete\":true}\n" {
            break;
        }
        assert!(Instant::now() < drained_by);
        std::thread::sleep(Duration::from_millis(20));
    }
    println!("diagnostic factory cleanup_complete=true");
}

fn replay_cases(
    directory: &Path,
    stage: &Path,
    admission_path: &Path,
    config_digest: &str,
    admission: &v2::AdmissionPolicy,
) {
    const REFERENCE: &[u8] = br#"#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d%d", &a, &b) != 2) return 2;
    printf("%d\n", a + b);
    return 0;
}
"#;
    const CHECKER: &[u8] = br#"#include <stdio.h>
int main(void) {
    int a, b;
    char trailing;
    return scanf("%d%d", &a, &b) != 2 || b <= 0 || scanf(" %c", &trailing) != EOF;
}
"#;
    const CANDIDATE: &[u8] = br#"#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d%d", &a, &b) != 2) return 2;
    printf("%d\n", a + b + (a == 8));
    return 0;
}
"#;
    const REPEATED_WRONG: &[u8] = br#"#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d%d", &a, &b) != 2) return 2;
    printf("%d\n", a + b + 1);
    return 0;
}
"#;
    const BOUNDARY: &[u8] = br#"#include <stdio.h>
#ifndef __OPTIMIZE__
#error Boundary sampling requires the formal optimized profile
#endif
static volatile unsigned char buffer[32 * 1024 * 1024];
int main(void) {
    int a, b;
    if (scanf("%d%d", &a, &b) != 2) return 2;
    if (a == 8) {
        volatile unsigned long sum = 0;
        for (unsigned long i = 0; i < 100000000; ++i) sum += i;
        printf("%lu\n", sum);
    }
    if (a == 0)
        for (unsigned long i = 0; i < sizeof(buffer); i += 4096) buffer[i] = 1;
    printf("%d\n", a + b);
    return 0;
}
"#;
    let artifacts = directory.join("artifacts");
    fs::create_dir(&artifacts).unwrap();
    fs::set_permissions(&artifacts, fs::Permissions::from_mode(0o700)).unwrap();
    chown(&artifacts, Some(1000), Some(1000)).unwrap();
    let save = |bytes: &[u8]| {
        let identity = reference(bytes);
        private(&artifacts.join(identity.digest.as_str()), bytes, true);
        identity
    };
    let cases = [
        (b"-3 10\n".as_slice(), b"7\n".as_slice()),
        (b"8 5\n".as_slice(), b"13\n".as_slice()),
        (b"0 0\n".as_slice(), b"0\n".as_slice()),
    ]
    .into_iter()
    .map(|(data, expected)| v1::TestCase {
        case_id: id(),
        input: save(data),
        expected: save(expected),
    })
    .collect::<Vec<_>>();
    let job_path = directory.join("job.json");
    let version = id();
    let worker_id = id();
    for (source, wanted) in [
        (REFERENCE, Some(v1::Verdict::Accepted)),
        (CANDIDATE, Some(v1::Verdict::WrongAnswer)),
        (BOUNDARY, None),
        (CHECKER, None),
        (REPEATED_WRONG, Some(v1::Verdict::WrongAnswer)),
    ] {
        let binding = v1::LeaseBinding {
            evaluation_id: id(),
            attempt_id: id(),
            worker_id,
            fencing_version: 1,
        };
        let capability = &admission.capabilities[0];
        let mut manifest = v2::JobManifest {
            schema_version: v2::SchemaVersion,
            kind: v1::ManifestKind::Value,
            evaluation_id: binding.evaluation_id,
            submission_id: id(),
            problem_version_id: version,
            language: capability.language,
            toolchain: capability.toolchain.clone(),
            source: save(source),
            cases: cases.clone(),
            self_test: false,
            comparison: v1::Comparison::Value,
            compile_limits: capability.compile_ceiling.clone(),
            run_limits: capability.run_ceiling.clone(),
            job_wall_time_us: 40_000_000,
        };
        let repeated = source == REPEATED_WRONG;
        if repeated {
            manifest.cases.truncate(2);
            let repeats: Vec<_> = manifest
                .cases
                .iter()
                .map(|case| v1::TestCase {
                    case_id: id(),
                    ..case.clone()
                })
                .collect();
            manifest.cases.extend(repeats);
        }
        if wanted.is_none() {
            for case in &mut manifest.cases {
                // The resource Worker never requests an expected output.
                case.expected = reference(b"");
            }
            manifest.run_limits.cpu_time_us = 80_000;
            manifest.run_limits.memory_bytes = 16 * 1024 * 1024;
            if source == CHECKER {
                manifest.compile_limits.wall_time_us = 2_000_000;
                manifest.compile_limits.cpu_time_us = 2_000_000;
                manifest.run_limits.wall_time_us = 250_000;
                manifest.job_wall_time_us = 3_000_000;
            }
        }
        let manifest_digest = format!(
            "{:x}",
            Sha256::digest(v2::manifest_bytes(&manifest).unwrap())
        );
        private(
            &job_path,
            &serde_json::to_vec(&serde_json::json!({
                "manifest": manifest,
                "binding": binding,
                "manifest_digest": manifest_digest,
                "attempt_number": 1
            }))
            .unwrap(),
            true,
        );
        let mut command = ordinary();
        command
            .args(["replay-diagnostic-batch", "--socket", FACTORY_SOCKET])
            .arg("--job")
            .arg(&job_path)
            .arg("--artifacts")
            .arg(&artifacts)
            .arg("--staging")
            .arg(stage)
            .arg("--admission")
            .arg(admission_path)
            .args(["--worker-id", &worker_id.as_uuid().to_string()])
            .args(["--factory-config-sha256", config_digest]);
        if wanted.is_none() {
            command.arg("--measure-resources");
        }
        let mut worker = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let control = worker.stdin.take().unwrap();
        let output = worker.wait_with_output().unwrap();
        drop(control);
        assert!(output.status.success());
        if let Some(wanted) = wanted {
            let report: v2::JudgementReport = decode_wire(&output.stdout).unwrap();
            assert_eq!(report.binding, binding);
            assert_eq!(report.verdict, wanted);
            if repeated {
                assert_eq!(report.cases.len(), 4);
                assert!(report.cases.iter().all(|case| case.verdict == wanted));
            } else {
                assert_eq!(report.cases.len(), cases.len());
                assert_eq!(report.cases[0].verdict, v1::Verdict::Accepted);
                assert_eq!(report.cases[1].verdict, wanted);
                assert_eq!(report.cases[2].verdict, v1::Verdict::Accepted);
            }
            assert!(
                report
                    .cases
                    .iter()
                    .all(|case| { case.cpu_time_us.is_some() && case.memory_peak_bytes.is_some() })
            );
            assert!(report.cleanup_complete && report.failure_code.is_none());
            assert!(report.compilation.diagnostics.as_str().is_empty());
            assert!(report.self_test_output.is_none());
        } else {
            let report: DiagnosticBoundaryReport = decode_wire(&output.stdout).unwrap();
            assert_eq!(report.binding, binding);
            assert_eq!(report.manifest_digest.as_str(), manifest_digest);
            assert_eq!(report.cases.len(), cases.len());
            let expected = if source == CHECKER {
                [
                    BoundaryOutcome::Completed,
                    BoundaryOutcome::Completed,
                    BoundaryOutcome::RuntimeError,
                ]
            } else {
                [
                    BoundaryOutcome::Completed,
                    BoundaryOutcome::TimeLimit,
                    BoundaryOutcome::MemoryLimit,
                ]
            };
            let label = if source == CHECKER {
                "KF_INPUT_CHECK"
            } else {
                "KF_BOUNDARY"
            };
            for ((observed, original), expected) in report.cases.iter().zip(&cases).zip(expected) {
                assert_eq!(observed.case_id, original.case_id);
                println!(
                    "{label} {:?} cpu_time_us={} memory_peak_bytes={}",
                    observed.outcome, observed.cpu_time_us, observed.memory_peak_bytes,
                );
                assert_eq!(observed.outcome, expected);
                assert!(observed.cpu_time_us > 0 && observed.memory_peak_bytes > 0);
            }
        }
        assert_eq!(fs::read_dir(stage).unwrap().count(), 0);
        let cleanup = ordinary()
            .args(["check-stopped", "--socket", FACTORY_SOCKET])
            .args([
                "--factory-config-sha256",
                config_digest,
                "--diagnostic-staging",
            ])
            .arg(stage)
            .output()
            .unwrap();
        assert!(cleanup.status.success());
        assert_eq!(cleanup.stdout, b"{\"cleanup_complete\":true}\n");
        if repeated {
            println!(
                "KF_REDUCTION original_runs=2 smaller_runs=2 all_wrong_answer=true cleanup=true"
            );
        } else if let Some(wanted) = wanted {
            println!(
                "KF_BATCH_REPLAY {wanted:?} all_three_cases=true resource_measurements=true cleanup=true",
            );
        } else if source == CHECKER {
            println!("KF_INPUT_CHECK_CLEANUP=true");
        } else {
            println!("KF_BOUNDARY_CLEANUP=true");
        }
    }
}

#[test]
#[ignore = "independent VM with an idle test factory; no user program is executed"]
fn diagnostic_recovery_requires_bound_factory_and_verified_staging() {
    support::require_vm();
    let root = support::Root::new();
    let folder = GuestDirectory(PathBuf::from(format!(
        "/kf/recovery-{}",
        std::process::id()
    )));
    fs::create_dir(&folder.0).unwrap();
    fs::set_permissions(&folder.0, fs::Permissions::from_mode(0o711)).unwrap();
    let stage = folder.0.join("stage");
    fs::create_dir(&stage).unwrap();
    chown(&stage, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o700)).unwrap();
    let root_path = folder.0.join("root.json");
    let config = serde_json::to_vec(&root.config).unwrap();
    let config_digest = format!("{:x}", Sha256::digest(&config));
    private(&root_path, &config, false);
    let _factory = start_factory(&root_path);
    let check_cleanup = |digest: &str| {
        ordinary()
            .args(["check-stopped", "--socket", FACTORY_SOCKET])
            .args(["--factory-config-sha256", digest, "--diagnostic-staging"])
            .arg(&stage)
            .output()
            .unwrap()
    };
    assert!(!check_cleanup(&"0".repeat(64)).status.success());
    let mut updated_config = config.clone();
    updated_config.push(b'\n');
    private(&root_path, &updated_config, false);
    assert!(!check_cleanup(&config_digest).status.success());
    private(&root_path, &config, false);
    println!("diagnostic proof refused both an unrelated digest and changed factory configuration");

    let foreign = stage.join("foreign");
    fs::write(&foreign, b"fixture-owned").unwrap();
    assert!(!check_cleanup(&config_digest).status.success());
    assert_eq!(fs::read(&foreign).unwrap(), b"fixture-owned");
    fs::remove_file(foreign).unwrap();
    println!("diagnostic proof refused and preserved an unrecognized staging entry");

    let orphan = stage.join("orphaned-stage");
    fs::create_dir(&orphan).unwrap();
    chown(&orphan, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(&orphan, fs::Permissions::from_mode(0o700)).unwrap();
    let inode = fs::metadata(&orphan).unwrap();
    let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id").unwrap();
    let journal = serde_json::json!({
        "pid": 2147483647_i32, "ticks": 1, "boot": boot.trim(), "dev": inode.dev(), "ino": inode.ino(),
    });
    private(
        &orphan.join(".owner"),
        format!("{journal}\n").as_bytes(),
        true,
    );
    let proof = check_cleanup(&config_digest);
    assert!(proof.status.success());
    assert_eq!(proof.stdout, b"{\"cleanup_complete\":true}\n");
    assert!(fs::read_dir(&stage).unwrap().next().is_none());
    println!("diagnostic proof accepted after removing the journaled orphan");
}
