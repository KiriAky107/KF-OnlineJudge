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
    assert_eq!(v["report"]["cleanup"], "complete", "{v}");
    assert_eq!(v["report"]["errors"], serde_json::json!([]), "{v}");
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
    for dir in ["tasks", "identities", "captures"] {
        assert_eq!(
            fs::read_dir(format!("/var/lib/kf-judge/{dir}"))
                .unwrap()
                .count(),
            0
        );
    }
    println!("KF_RESOURCE_EXECUTION_PROOF {v}");
    v
}
fn limited(v: &serde_json::Value, kind: &str) {
    assert_eq!(
        v["outcome"],
        serde_json::json!({"kind":"limit_exceeded", "primary":kind}),
        "{v}"
    );
    assert!(
        v["report"]["limits"]
            .as_array()
            .unwrap()
            .iter()
            .any(|l| l["evidence"]["kind"] == kind)
    );
}
#[test]
#[ignore = "independent capped VM: aggregate busy child threads, monotonic sleep and actual group OOM"]
fn cpu_wall_and_group_oom() {
    let cpu = run("resource-cpu");
    limited(&cpu, "cpu_time");
    assert!(
        cpu["report"]["usage"]["cpu_time_us"]["value"]
            .as_u64()
            .unwrap()
            >= 500000
    );
    assert!(cpu["proof"]["reaped_orphans"].as_u64().unwrap() > 0);
    assert!(
        cpu["stdout_prefix_hex"]
            .as_str()
            .unwrap()
            .starts_with("4350555f4348494c445f544852454144535f5245414459")
    );
    limited(&run("wall"), "wall_time");
    let oom = run("resource-oom");
    limited(&oom, "memory");
    let events = &oom["report"]["usage"]["memory_events"]["value"];
    for key in ["max", "oom", "oom_kill", "oom_group_kill"] {
        assert!(events[key].as_u64().unwrap() > 0, "{oom}");
    }
    assert!(events["oom_kill"].as_u64().unwrap() >= 2, "{oom}");
}
#[test]
#[ignore = "independent capped VM: finite fork/pthread growth reaches task pids.max and full tree stop"]
fn process_and_thread_limits_use_trusted_kernel_counters() {
    for case in ["resource-fork", "resource-threads"] {
        let v = run(case);
        limited(&v, "processes");
        assert!(v["report"]["limits"].as_array().unwrap().iter().any(|l| {
            l["evidence"]["max_events_delta"]
                .as_u64()
                .is_some_and(|n| n > 0)
        }));
    }
}
#[test]
#[ignore = "independent capped VM: stream floods, real EFBIG/ENOSPC/EMFILE and finite retained data"]
fn output_file_space_and_descriptor_limits() {
    for case in ["stdout", "stderr"] {
        let v = run(case);
        limited(&v, case);
        assert_eq!(v["report"][case]["retained_bytes"], 4096);
        assert_eq!(v["report"][case]["observed_bytes"]["value"], 4097);
        assert_eq!(v["report"][case]["truncated"], true);
        assert!(v["proof"]["reaped_orphans"].as_u64().unwrap() > 0);
    }
    for (case, expected) in [
        ("disk", "KF_DISK_PROOF file=1 work_enospc=1 tmp_enospc=1\n"),
        (
            "resource-fds",
            "FD_PROOF soft=64 hard=64 opened=61 errno=24\n",
        ),
    ] {
        let v = run(case);
        assert_eq!(
            v["outcome"],
            serde_json::json!({"kind":"exited","code":0}),
            "{v}"
        );
        assert_eq!(v["report"]["limits"], serde_json::json!([]));
        let hex: String = expected.bytes().map(|b| format!("{b:02x}")).collect();
        assert_eq!(v["stdout_prefix_hex"], hex);
        assert_eq!(v["report"]["stdout"]["retained_bytes"], expected.len());
    }
}
