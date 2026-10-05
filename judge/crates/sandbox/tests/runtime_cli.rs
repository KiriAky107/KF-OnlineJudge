use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU32, Ordering};

const EXAMPLE: &str = include_str!("../../../../deploy/sandbox.runtime.example.json");
static SEQUENCE: AtomicU32 = AtomicU32::new(0);

struct ConfigFile(PathBuf);
impl ConfigFile {
    fn new(contents: &[u8]) -> Self {
        let path = std::env::temp_dir().join(format!(
            "kf-runtime-{}-{}.json",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)
            .unwrap();
        file.write_all(contents).unwrap();
        Self(path)
    }
    fn check(&self) -> serde_json::Value {
        let output = Command::new(env!("CARGO_BIN_EXE_kf-sandbox"))
            .args(["check-runtime", "--config"])
            .arg(&self.0)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        serde_json::from_slice(&output.stderr).unwrap()
    }
}
impl Drop for ConfigFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

#[test]
fn runtime_cli_reads_example_but_reports_unverified_capabilities_and_refuses() {
    let report = ConfigFile::new(EXAMPLE.as_bytes()).check();
    assert_eq!(report["configuration_valid"], true);
    assert_eq!(report["execution_allowed"], false);
    assert_eq!(report["error"], "runtime_not_ready");
    assert!(
        report["reasons"]
            .as_array()
            .unwrap()
            .iter()
            .any(|reason| reason["code"] == "sandbox_not_implemented")
    );
}

#[test]
fn runtime_config_size_boundary_and_invalid_json_are_structured_without_echoing_content() {
    let mut exact = EXAMPLE.as_bytes().to_vec();
    exact.resize(kf_sandbox::runtime::MAX_CONFIG_BYTES as usize, b' ');
    assert_eq!(ConfigFile::new(&exact).check()["configuration_valid"], true);
    exact.push(b' ');
    assert_eq!(
        ConfigFile::new(&exact).check()["error"],
        "runtime_config_too_large"
    );
    let report = ConfigFile::new(br#"{"secret":"PRIVATE-CONFIG", "broken":}"#).check();
    assert_eq!(report["error"], "invalid_runtime_config_json");
    assert!(!report.to_string().contains("PRIVATE-CONFIG"));
}

#[test]
fn runtime_cli_rejects_invalid_policy_and_missing_configuration() {
    let mut invalid: serde_json::Value = serde_json::from_str(EXAMPLE).unwrap();
    invalid["identities"]["worker_uid"] = serde_json::json!(0);
    let report = ConfigFile::new(serde_json::to_string(&invalid).unwrap().as_bytes()).check();
    assert_eq!(report["error"], "invalid_runtime_config");
    assert_eq!(report["details"]["field"], "identities");
    let missing = ConfigFile::new(EXAMPLE.as_bytes());
    fs::remove_file(&missing.0).unwrap();
    assert_eq!(missing.check()["error"], "cannot_read_runtime_config");
}
