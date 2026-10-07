#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use std::{fs, path::Path, process::Command};
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
    assert_eq!(v["report"]["cleanup"], "complete", "{v}");
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
    let root = Path::new("/var/lib/kf-judge");
    for dir in ["tasks", "identities", "captures"] {
        assert_eq!(
            fs::read_dir(root.join(dir)).unwrap().count(),
            0,
            "{case} {dir}"
        );
    }
    for dir in ["/sys/fs/cgroup", "/kf"] {
        for entry in fs::read_dir(dir).unwrap() {
            let n = entry.unwrap().file_name();
            assert!(
                !n.to_string_lossy().starts_with("kf-acceptance-")
                    && !n.to_string_lossy().starts_with("execution-publications-")
            );
        }
    }
    println!("KF_EXECUTION_PROOF {v}");
    v
}
#[test]
#[ignore = "independent capped VM: actual private-root binary pipes and final sealed export"]
fn binary_backpressure_and_final_export() {
    let v = run("binary");
    assert_eq!(v["outcome"], serde_json::json!({"kind":"exited","code":7}));
    assert_eq!(v["report"]["stdout"]["retained_bytes"], 65537);
    assert_eq!(v["report"]["stderr"]["retained_bytes"], 8193);
    assert_eq!(v["outputs"][0]["bytes"], 32769);
    assert!(
        v["stdout_prefix_hex"]
            .as_str()
            .unwrap()
            .starts_with("000102030405")
    );
    assert!(
        v["outputs"][0]["prefix_hex"]
            .as_str()
            .unwrap()
            .starts_with("000102030405")
    );
}
#[test]
#[ignore = "independent capped VM: stdout/stderr flood and whole descendant termination"]
fn floods_are_bounded_and_kill_descendants() {
    for case in ["stdout", "stderr"] {
        let v = run(case);
        assert_eq!(v["outcome"]["kind"], "limit_exceeded", "{v}");
        assert_eq!(v["outcome"]["primary"], case);
        assert_eq!(v["report"][case]["retained_bytes"], 4096);
        assert_eq!(v["report"][case]["observed_bytes"]["value"], 4097);
        assert_eq!(v["report"][case]["truncated"], true);
        assert!(v["proof"]["reaped_orphans"].as_u64().unwrap() > 0);
    }
}
#[test]
#[ignore = "independent capped VM: timeout/cancel/monitor fault unified shutdown and final meters"]
fn wall_cancel_monitor_and_main_descendant_cleanup() {
    for case in ["wall", "cancel", "monitor", "leftover"] {
        let v = run(case);
        assert_eq!(
            v["outcome"]["kind"],
            match case {
                "wall" => "limit_exceeded",
                "cancel" => "cancelled",
                "monitor" => "infrastructure_failure",
                _ => "exited",
            },
            "{v}"
        );
        assert!(v["proof"]["reaped_orphans"].as_u64().unwrap() > 0, "{v}");
        if case == "leftover" {
            assert_eq!(v["report"]["termination"]["code"], 7);
        }
    }
}
#[test]
#[ignore = "independent capped VM: early stdin closure, finite file/work/tmp and all-or-none export"]
fn stdin_disk_and_export_failure() {
    let v = run("early");
    assert_eq!(v["outcome"]["kind"], "exited");
    assert_eq!(v["proof"]["stdin_closed_early"], true);
    let v = run("disk");
    assert_eq!(
        v["outcome"],
        serde_json::json!({"kind":"exited","code":0}),
        "{v}"
    );
    let text = hex(&v["stdout_prefix_hex"]);
    assert!(text.contains("file=1 work_enospc=1 tmp_enospc=1"), "{text}");
    let v = run("export");
    assert_eq!(v["outcome"]["kind"], "infrastructure_failure");
    assert_eq!(v["report"]["artifacts"], serde_json::json!([]));
    assert_eq!(v["report"]["errors"][0]["stage"], "artifact_export");
}
fn hex(v: &serde_json::Value) -> String {
    let s = v.as_str().unwrap();
    String::from_utf8(
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect(),
    )
    .unwrap()
}
