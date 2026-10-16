#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use std::{fs, process::Command};
fn run(case: &str) -> serde_json::Value {
    support::require_vm();
    let output = Command::new(support::binary())
        .args(["vm-case", case])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{case}: {} {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(v["outside_sentinels_unchanged"], true);
    for dir in ["tasks", "identities", "captures"] {
        assert_eq!(
            fs::read_dir(format!("/var/lib/kf-judge/{dir}"))
                .unwrap()
                .count(),
            0
        );
    }
    println!("KF_ISOLATION_FILES_PROOF {v}");
    v
}
#[test]
#[ignore = "independent capped VM: actual user proc/dev/input/FD/root escape boundary"]
fn full_user_filesystem_and_descriptor_boundary() {
    let v = run("isolation-files");
    assert_eq!(
        v["outcome"],
        serde_json::json!({"kind":"exited","code":0}),
        "{v}"
    );
    assert_eq!(v["report"]["cleanup"], "complete");
    assert_eq!(v["report"]["errors"], serde_json::json!([]));
    let expected = "FILES_OK proc dev input stdin fd root image\n";
    let hex: String = expected.bytes().map(|b| format!("{b:02x}")).collect();
    assert_eq!(v["stdout_prefix_hex"], hex);
}
#[test]
#[ignore = "independent capped VM: caller directory without CLOEXEC must refuse before exec"]
fn unexpected_inherited_directory_is_refused_and_cleaned() {
    let v = run("isolation-inherited-fd");
    assert_eq!(v["refused_before_exec"], true);
    assert!(v["cleanup_failure"].is_null());
}
