#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use std::{fs, path::Path, process::Command};
fn check(case: &str, deny: bool) -> serde_json::Value {
    support::require_vm();
    let before = support::children();
    let output = if deny {
        Command::new("/kf/bin/rollback-deny")
            .arg(case.strip_prefix("fail-").unwrap())
            .env_clear()
            .env("KF_SANDBOX_INDEPENDENT_VM", "1")
            .output()
            .unwrap()
    } else {
        Command::new(support::binary())
            .args(["vm-case", case])
            .env_clear()
            .env("KF_SANDBOX_INDEPENDENT_VM", "1")
            .output()
            .unwrap()
    };
    assert!(
        output.status.success(),
        "{case}: {} {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(support::children(), before);
    for path in [
        "/var/lib/kf-judge/tasks",
        "/var/lib/kf-judge/identities",
        "/var/lib/kf-judge/captures",
    ] {
        if Path::new(path).exists() {
            assert_eq!(fs::read_dir(path).unwrap().count(), 0, "{case} {path}");
        }
    }
    for path in ["/sys/fs/cgroup", "/kf"] {
        for entry in fs::read_dir(path).unwrap() {
            let n = entry.unwrap().file_name();
            assert!(
                !n.to_string_lossy().starts_with("kf-acceptance-")
                    && !n.to_string_lossy().starts_with("execution-publications-"),
                "{case} {n:?}"
            );
        }
    }
    println!("KF_ROLLBACK_PROOF {value}");
    value
}
#[test]
#[ignore = "independent capped VM: cancellation/drop after every typed preparation checkpoint"]
fn cancel_all_preparation_gates_without_exec_or_leaks() {
    for stage in [
        "blocked",
        "namespace",
        "root",
        "proc",
        "inputs",
        "identity",
        "seccomp",
        "io",
        "exec",
    ] {
        let v = check(&format!("cancel-{stage}"), false);
        assert_eq!(v["cancel_before_exec"], true);
        assert_eq!(v["remaining_owned_children"], 0);
        if matches!(stage, "blocked" | "io" | "exec") {
            assert_eq!(v["idempotent"], true);
        }
    }
}
#[test]
#[ignore = "independent capped VM: actual kernel failures in six successive isolation stages"]
fn real_kernel_stage_failures_rollback_owned_resources() {
    for (stage, expected) in [
        ("namespace", "namespaces"),
        ("root", "root_filesystem"),
        ("proc", "root_filesystem"),
        ("rlimits", "preparation"),
        ("identity", "identity"),
        ("seccomp", "seccomp"),
    ] {
        let v = check(&format!("fail-{stage}"), true);
        assert_eq!(v["failure"]["stage"], expected, "{v}");
        assert_eq!(v["failure"]["errno"], 1, "{v}");
        assert_eq!(v["cleanup_complete"], true);
        assert_eq!(v["user_exec_released"], false);
    }
}
#[test]
#[ignore = "independent capped VM: post-root missing input and post-filter pipe budget failure"]
fn late_data_failures_preserve_stage_and_clean() {
    for case in ["fail-inputs", "fail-io"] {
        let v = check(case, false);
        assert_eq!(v["cleanup_complete"], true);
        assert_eq!(v["user_exec_released"], false);
        assert_eq!(
            v["failure"]["stage"],
            if case == "fail-inputs" {
                "root_filesystem"
            } else {
                "exec"
            }
        );
    }
}
