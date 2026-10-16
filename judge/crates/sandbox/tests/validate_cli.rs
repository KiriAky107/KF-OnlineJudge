use kf_sandbox::spec::MAX_SPEC_BYTES;
use serde_json::{Value, json};
use std::fs;
use std::path::PathBuf;
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};

static SEQUENCE: AtomicUsize = AtomicUsize::new(0);

struct SpecFile {
    directory: PathBuf,
    path: PathBuf,
}

impl SpecFile {
    fn new(bytes: &[u8]) -> Self {
        let directory = std::env::temp_dir().join(format!(
            "kf-spec-test-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("spec.json");
        fs::write(&path, bytes).unwrap();
        Self { directory, path }
    }

    fn validate(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_kf-sandbox"))
            .arg("validate")
            .arg("--spec")
            .arg(&self.path)
            .output()
            .unwrap()
    }
}

impl Drop for SpecFile {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).unwrap();
    }
}

fn refused(output: Output, code: &str) -> Value {
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let error: Value = serde_json::from_slice(&output.stderr).unwrap();
    assert_eq!(error["error"], code);
    error
}

#[test]
fn valid_request_at_size_boundary_is_checked_without_execution() {
    let mut bytes = include_bytes!("fixtures/spec.valid.json").to_vec();
    bytes.resize(MAX_SPEC_BYTES as usize, b' ');
    let output = SpecFile::new(&bytes).validate();
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        report,
        json!({"valid": true, "execution_allowed": false, "run_id": "run-001"})
    );
}

#[test]
fn oversized_or_unreadable_spec_is_refused() {
    refused(
        SpecFile::new(&vec![b' '; MAX_SPEC_BYTES as usize + 1]).validate(),
        "spec_too_large",
    );
    let file = SpecFile::new(b"{}");
    fs::remove_file(&file.path).unwrap();
    refused(file.validate(), "cannot_read_spec");
}

#[test]
fn parse_error_does_not_echo_untrusted_request_contents() {
    let file = SpecFile::new(br#"{"profile":"SECRET-UNSUPPORTED-PROFILE"}"#);
    let output = file.validate();
    assert!(!String::from_utf8_lossy(&output.stderr).contains("SECRET"));
    refused(output, "invalid_spec_json");
}

#[test]
fn invalid_limits_produce_field_specific_validation_error() {
    let mut document: Value =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    document["limits"]["memory_bytes"] = json!(0);
    let file = SpecFile::new(&serde_json::to_vec(&document).unwrap());
    let report = refused(file.validate(), "invalid_execution_spec");
    assert_eq!(
        report["details"],
        json!({"field": "memory_bytes", "code": "invalid_limit"})
    );
}
