#![cfg(target_os = "linux")]
#[allow(dead_code)]
mod support;
use std::process::Command;
#[test]
#[ignore = "requires independent capped VM with fixed namespace role fixture"]
fn namespace_init_reaps_main_and_orphans_and_cancels_surviving_tree() {
    support::require_vm();
    for mode in [
        "normal",
        "orphan-short",
        "orphan-cancel",
        "orphan-disconnect",
        "exec-fail",
    ] {
        let output = Command::new("/kf/bin/tree-probe")
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
        assert!(proof.contains(&format!("KF_TREE_PROOF mode={mode}")));
        print!("{proof}");
    }
}
