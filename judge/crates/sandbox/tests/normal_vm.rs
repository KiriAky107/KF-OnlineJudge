#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use std::{fs, process::Command};

fn check_run(v: &serde_json::Value, code: u8) {
    assert_eq!(
        v["outcome"],
        serde_json::json!({"kind":"exited", "code":code}),
        "{v}"
    );
    assert_eq!(v["report"]["cleanup"], "complete", "{v}");
    assert_eq!(v["report"]["errors"], serde_json::json!([]));
    assert_eq!(v["report"]["limits"], serde_json::json!([]));
    assert_eq!(v["immutable_snapshots_verified"], true);
    assert_eq!(
        v["proof"]["order"],
        serde_json::json!([
            "verified_isolation",
            "exec_gate",
            "cgroup_empty",
            "owned_reaped",
            "final_accounting",
            "final_export",
            "resource_removal"
        ])
    );
    for metric in [
        "cpu_time_us",
        "wall_time_us",
        "memory_peak_bytes",
        "memory_events",
    ] {
        assert_eq!(v["report"]["usage"][metric]["state"], "measured", "{v}");
    }
}
fn run(case: &str, code: u8) -> serde_json::Value {
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
    check_run(&v, code);
    for dir in ["tasks", "identities", "captures"] {
        assert_eq!(
            fs::read_dir(format!("/var/lib/kf-judge/{dir}"))
                .unwrap()
                .count(),
            0
        );
    }
    println!("KF_NORMAL_PROOF {v}");
    v
}
#[test]
#[ignore = "independent capped VM: static and dynamic C/C++ in actual private root"]
fn static_and_dynamic_language_programs() {
    for case in ["normal-static", "normal-c", "normal-cpp"] {
        assert_eq!(run(case, 0)["complete_stdout_content_verified"], true);
    }
}
#[test]
#[ignore = "independent capped VM: sandbox compiler exported ELF runs as a new task"]
fn sandbox_compilation_then_artifact_execution() {
    for case in ["normal-compile-c", "normal-compile-cpp"] {
        let v = run(case, 0);
        check_run(&v["compilation"], 0);
        assert_eq!(v["compilation"]["outputs"][0]["path"], "main.bin");
        assert!(
            v["compilation"]["outputs"][0]["prefix_hex"]
                .as_str()
                .unwrap()
                .starts_with("7f454c46")
        );
        assert_ne!(v["compilation"]["report"]["run_id"], v["report"]["run_id"]);
    }
}
#[test]
#[ignore = "independent capped VM: empty stdin EOF, binary capture/export and nonzero exit"]
fn empty_binary_and_nonzero_are_real_results() {
    assert_eq!(
        run("normal-empty", 0)["report"]["stdout"]["retained_bytes"],
        10
    );
    run("normal-nonzero", 23);
    let v = run("binary", 7);
    assert_eq!(v["complete_binary_content_verified"], true);
    assert_eq!(v["report"]["stdout"]["retained_bytes"], 65537);
    assert_eq!(v["outputs"][0]["bytes"], 32769);
}
