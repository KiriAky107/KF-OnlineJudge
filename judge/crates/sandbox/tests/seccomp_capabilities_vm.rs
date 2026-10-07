#![cfg(target_os = "linux")]
#[allow(dead_code)]
mod support;
use std::process::Command;
fn fixture(mode: &str, proof: &str) {
    support::require_vm();
    let output = Command::new("/kf/bin/seccomp-probe")
        .arg(mode)
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{mode}: {:?} {:?}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains(proof), "{stdout}");
    print!("{stdout}");
}
#[test]
#[ignore = "requires independent capped VM"]
fn reduced_identity_positive_controls_and_all_profile_danger_restrictions() {
    fixture("baseline", "KF_SECCOMP_BASELINE_PROOF");
    for mode in ["danger-native", "danger-c", "danger-cpp"] {
        fixture(mode, "KF_SECCOMP_DANGER_PROOF");
    }
}
#[test]
#[ignore = "requires independent capped VM with fixed read-only GNU payload"]
fn real_c_cpp_compilers_and_native_dynamic_binary_io_threads() {
    fixture("language-c", "KF_SECCOMP_LANGUAGE_PROOF profile=compile-c");
    fixture(
        "language-cpp",
        "KF_SECCOMP_LANGUAGE_PROOF profile=compile-cpp",
    );
    fixture(
        "language-native",
        "KF_SECCOMP_LANGUAGE_PROOF profile=native",
    );
}
