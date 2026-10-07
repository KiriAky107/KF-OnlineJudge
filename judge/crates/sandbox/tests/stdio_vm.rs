#![cfg(target_os = "linux")]
#[allow(dead_code)]
mod support;
use std::process::Command;
#[test]
#[ignore = "requires independent capped VM with fixed namespace role fixture"]
fn independent_stdio_drains_binary_backpressure_eof_early_close_and_limits() {
    support::require_vm();
    for mode in [
        "duplex",
        "empty",
        "early-close",
        "stdout-limit",
        "stderr-limit",
        "storage-fail",
    ] {
        let output = Command::new("/kf/bin/io-probe")
            .args(["fixed", mode])
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
        let proof = String::from_utf8(output.stdout).unwrap();
        assert!(proof.contains(&format!("KF_IO_PROOF mode={mode}")));
        print!("{proof}");
    }
}

#[test]
#[ignore = "requires independent capped VM; SIGPIPE default may terminate a broken monitor"]
fn early_close_contains_sigpipe_without_global_disposition_or_mask_leak() {
    support::require_vm();
    let result = std::process::Command::new("/kf/bin/io-probe")
        .args(["signal-mask", "early-close"])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{:?} {:?}",
        result.stdout,
        result.stderr
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains("KF_IO_SIGPIPE_PROOF"));
    print!("{}", String::from_utf8_lossy(&result.stdout));
}
