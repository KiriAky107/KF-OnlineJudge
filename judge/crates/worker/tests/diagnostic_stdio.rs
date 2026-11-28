#![cfg(target_os = "linux")]

use kf_contracts::{
    diagnostic::{DiagnosticResult, DiagnosticRun, ResultState},
    judging::{FailureCode, decode_wire},
};
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    time::{SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

struct TestDirectory(PathBuf);
impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn worker(root: &Path, run: &DiagnosticRun, source: &[u8]) -> io::Result<Output> {
    let header = serde_json::to_vec(run).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_kf-worker"))
        .args(["diagnose-public-sample", "--socket"])
        .arg(root.join("unavailable.sock"))
        .arg("--staging")
        .arg(root.join("stage"))
        .arg("--admission")
        .arg(root.join("admission.json"))
        .args([
            "--worker-id",
            run.binding.worker_id.as_uuid().to_string().as_str(),
        ])
        .args(["--factory-config-sha256", &"b".repeat(64)])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let mut stdin = child.stdin.take().unwrap();
    let sent = (|| {
        stdin.write_all(&(header.len() as u32).to_be_bytes())?;
        stdin.write_all(&header)?;
        stdin.write_all(source)?;
        stdin.write_all(b"1\n2\n")
    })();
    drop(stdin);
    if let Err(error) = sent
        && error.kind() != io::ErrorKind::BrokenPipe
    {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    child.wait_with_output()
}

#[test]
fn private_diagnostic_worker_refuses_invalid_bytes_pins_expiry_and_missing_factory() {
    // The missing factory is intentional. No compiler or submitted bytes are executed.
    let root =
        TestDirectory(std::env::temp_dir().join(format!("kf-diagnostic-{}", Uuid::new_v4())));
    fs::create_dir(&root.0).unwrap();
    fs::create_dir(root.0.join("stage")).unwrap();
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.0.join("admission.json"))
        .unwrap()
        .write_all(include_bytes!(
            "../../../../tests/fixtures/contracts-v2/judge-registry.valid.json"
        ))
        .unwrap();
    let mut run: DiagnosticRun = decode_wire(include_bytes!(
        "../../../../tests/fixtures/diagnostics-v1/diagnostic-run.valid.json"
    ))
    .unwrap();
    let source = b"int main(void) { return 0; }\n";
    let mut different = *source;
    different[0] ^= 1;
    assert!(!worker(&root.0, &run, &different).unwrap().status.success());
    let pinned = run.configuration.toolchain.image_digest.clone();
    run.configuration.toolchain.image_digest = "9".repeat(64).try_into().unwrap();
    assert!(!worker(&root.0, &run, source).unwrap().status.success());
    run.configuration.toolchain.image_digest = pinned;

    run.claim_expires_at_unix_ms = 1;
    let expired = worker(&root.0, &run, source).unwrap();
    assert!(expired.status.success());
    let report: DiagnosticResult = decode_wire(&expired.stdout).unwrap();
    assert_eq!(report.binding, run.binding);
    assert_eq!(report.state, ResultState::Failed);
    assert_eq!(report.failure_code, Some(FailureCode::JobDeadline));

    run.claim_expires_at_unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
        + 55_000;
    let missing_factory = worker(&root.0, &run, source).unwrap();
    assert!(missing_factory.status.success());
    let report: DiagnosticResult = decode_wire(&missing_factory.stdout).unwrap();
    assert_eq!(report.binding, run.binding);
    assert_eq!(report.state, ResultState::Failed);
    assert_eq!(report.failure_code, Some(FailureCode::SandboxUnavailable));
    assert_eq!(fs::read_dir(root.0.join("stage")).unwrap().count(), 0);
}
