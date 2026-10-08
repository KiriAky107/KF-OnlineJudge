use kf_sandbox::probe::EnvironmentObservation;
use kf_sandbox::runtime::{RuntimeConfig, kernel_supported, preflight};
use kf_sandbox::spec::{ExecutionSpec, SpecPolicy};

fn config() -> RuntimeConfig {
    serde_json::from_str(include_str!(
        "../../../../deploy/sandbox.runtime.example.json"
    ))
    .unwrap()
}

#[test]
fn example_has_distinct_identity_slots_and_applies_operator_task_ceiling() {
    let validated = config().validate().unwrap();
    assert_eq!(validated.task_identity(0), Some((100000, 100000)));
    assert_eq!(validated.task_identity(1), Some((100001, 100001)));
    assert_eq!(validated.task_identity(2), None);
    assert_eq!(validated.task_identity(u32::MAX), None);
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    request.limits.memory_bytes = 1024 * 1024 * 1024;
    assert!(request.clone().validate(&SpecPolicy::default()).is_ok());
    assert!(request.validate(&validated.spec_policy()).is_err());
    let json = serde_json::to_string(validated.config()).unwrap();
    assert_eq!(
        serde_json::from_str::<RuntimeConfig>(&json).unwrap(),
        *validated.config()
    );
}

#[test]
fn roots_reject_traversal_aliases_system_locations_and_overlapping_storage() {
    for path in [
        "/",
        "/var/lib/../kf-judge",
        "/var//lib/kf-judge",
        "/var/lib/kf-judge/",
        "relative",
        "/var/lib/a\\b",
        "/etc/kf-judge",
    ] {
        let mut policy = config();
        policy.state_root = path.into();
        assert!(policy.validate().is_err(), "{path}");
    }
    for path in [
        "/var/lib/kf-judge/tasks",
        "/var/lib/kf-judge/tasks/image",
        "/var/lib/kf-judge/identities",
        "/var/lib/kf-judge/identities/image",
        "/var/lib/kf-judge",
        "/srv/unrelated",
    ] {
        let mut policy = config();
        policy.image_root = path.into();
        assert!(policy.validate().is_err(), "{path}");
    }
    for path in [
        "/sys/fs/cgroup",
        "/sys/fs/cgroup2/job",
        "/tmp/cgroup",
        "/sys/fs/cgroup/../job",
    ] {
        let mut policy = config();
        policy.delegated_cgroup_root = path.into();
        assert!(policy.validate().is_err(), "{path}");
    }
}

#[test]
fn identity_pools_reject_root_collision_shortage_and_integer_overflow() {
    let invalid = [
        {
            let mut p = config();
            p.identities.worker_uid = 0;
            p
        },
        {
            let mut p = config();
            p.identities.worker_gid = u32::MAX;
            p
        },
        {
            let mut p = config();
            p.identities.count = 0;
            p
        },
        {
            let mut p = config();
            p.identities.count = 1;
            p
        },
        {
            let mut p = config();
            p.identities.first_task_uid = 65534;
            p
        },
        {
            let mut p = config();
            p.identities.worker_uid = 100001;
            p
        },
        {
            let mut p = config();
            p.identities.first_task_gid = u32::MAX;
            p
        },
        {
            let mut p = config();
            p.identities.first_task_uid = u32::MAX - 1;
            p
        },
        {
            let mut p = config();
            p.max_concurrent_runs = 33;
            p
        },
    ];
    for policy in invalid {
        assert!(policy.validate().is_err());
    }
}

#[test]
fn every_aggregate_reservation_must_cover_all_slots_and_supervision() {
    let invalid = [
        {
            let mut p = config();
            p.aggregate.memory_bytes = p.per_task_ceiling.memory_bytes * 2;
            p
        },
        {
            let mut p = config();
            p.aggregate.processes = p.per_task_ceiling.processes * 2;
            p
        },
        {
            let mut p = config();
            p.aggregate.workdir_bytes = p.per_task_ceiling.workdir_bytes * 2;
            p
        },
        {
            let mut p = config();
            p.aggregate.cpu_quota_us = p.aggregate.task_cpu_quota_us * 2;
            p
        },
        {
            let mut p = config();
            p.aggregate.supervisor_memory_bytes = 0;
            p
        },
        {
            let mut p = config();
            p.aggregate.supervisor_processes = 0;
            p
        },
        {
            let mut p = config();
            p.aggregate.supervisor_workdir_bytes = 0;
            p
        },
        {
            let mut p = config();
            p.aggregate.supervisor_cpu_quota_us = 0;
            p
        },
    ];
    for policy in invalid {
        assert!(policy.validate().is_err());
    }
    let mut exact = config();
    exact.aggregate.cpu_quota_us =
        exact.aggregate.task_cpu_quota_us * 2 + exact.aggregate.supervisor_cpu_quota_us;
    assert!(exact.validate().is_ok());
}

#[test]
fn overflow_huge_service_budgets_and_invalid_task_ceilings_are_rejected() {
    let invalid = [
        {
            let mut p = config();
            p.aggregate.task_cpu_quota_us = u64::MAX;
            p.aggregate.cpu_quota_us = u64::MAX;
            p
        },
        {
            let mut p = config();
            p.aggregate.supervisor_memory_bytes = u64::MAX;
            p.aggregate.memory_bytes = u64::MAX;
            p
        },
        {
            let mut p = config();
            p.aggregate.memory_bytes = 64 * 1024 * 1024 * 1024 + 1;
            p
        },
        {
            let mut p = config();
            p.aggregate.processes = 8193;
            p
        },
        {
            let mut p = config();
            p.aggregate.cpu_period_us = 0;
            p
        },
        {
            let mut p = config();
            p.per_task_ceiling.processes = 0;
            p
        },
        {
            let mut p = config();
            p.per_task_ceiling.memory_bytes = 3 * 1024 * 1024 * 1024;
            p
        },
    ];
    for policy in invalid {
        assert!(policy.validate().is_err());
    }
}

#[test]
fn kernel_policy_handles_release_suffixes_future_versions_and_unknown_values() {
    for (release, expected) in [
        ("6.6.0", Some(true)),
        ("6.5.99", Some(false)),
        ("5.19.0", Some(false)),
        ("7.2.9-1-cachyos", Some(true)),
        ("999.1.0", Some(true)),
        ("unknown", None),
        ("6.x.1", None),
        ("6", None),
    ] {
        assert_eq!(kernel_supported(release), expected, "{release}");
    }
}

#[test]
fn visible_interfaces_or_a_caller_isolation_flag_never_grant_execution() {
    let observation = EnvironmentObservation {
        os: "linux",
        architecture: "x86_64",
        kernel_release: Some("7.2.9-1-cachyos".into()),
        namespace_entries: ["user", "mnt", "pid", "ipc", "uts", "net", "cgroup"]
            .map(String::from)
            .to_vec(),
        cgroup_v2_visible: true,
        cgroup_v2_controllers: ["cpu", "memory", "pids"].map(String::from).to_vec(),
        current_process_seccomp_mode: Some(2),
        isolation_verified: true,
    };
    let report = preflight(&config().validate().unwrap(), &observation);
    assert!(!report.execution_allowed);
    assert!(report.configuration_valid);
    assert!(
        report
            .reasons
            .iter()
            .any(|reason| reason.code == "sandbox_not_implemented")
    );
    for subject in [
        "cgroup_delegation",
        "pidfd_wait",
        "seccomp_filter",
        "aggregate_budget_enforcement",
    ] {
        assert!(report.reasons.iter().any(|reason| reason.code=="capability_not_verified" && reason.subject==subject));
    }
}

#[test]
fn missing_or_unsupported_platform_capabilities_have_structured_reasons() {
    let observation = EnvironmentObservation {
        os: "windows",
        architecture: "aarch64",
        kernel_release: Some("6.5.0".into()),
        namespace_entries: vec![],
        cgroup_v2_visible: false,
        cgroup_v2_controllers: vec![],
        current_process_seccomp_mode: None,
        isolation_verified: false,
    };
    let report = preflight(&config().validate().unwrap(), &observation);
    for (code, subject) in [
        ("unsupported_platform", "os"),
        ("unsupported_platform", "architecture"),
        ("kernel_below_policy_minimum", "kernel"),
        ("namespace_not_visible", "net"),
        ("cgroup_v2_not_visible", "cgroup"),
        ("controller_not_visible", "memory"),
    ] {
        assert!(
            report
                .reasons
                .iter()
                .any(|reason| reason.code == code && reason.subject == subject)
        );
    }
}

#[test]
fn configuration_cannot_enable_execution_or_bypass_required_fields() {
    let valid = serde_json::to_value(config()).unwrap();
    let mut invalid = valid.clone();
    invalid["execution_enabled"] = serde_json::json!(true);
    assert!(serde_json::from_value::<RuntimeConfig>(invalid).is_err());
    for (pointer, value) in [
        ("/schema_version", serde_json::json!(2)),
        ("/image_kind", serde_json::json!("host")),
        ("/max_concurrent_runs", serde_json::json!(4294967296_u64)),
    ] {
        let mut invalid = valid.clone();
        *invalid.pointer_mut(pointer).unwrap() = value;
        assert!(serde_json::from_value::<RuntimeConfig>(invalid).is_err());
    }
    let mut invalid = valid;
    invalid.as_object_mut().unwrap().remove("aggregate");
    assert!(serde_json::from_value::<RuntimeConfig>(invalid).is_err());
}

#[test]
fn task_and_image_roots_cannot_overlap_private_capture_storage() {
    for field in ["tasks", "image"] {
        for path in [
            "/var/lib/kf-judge/captures",
            "/var/lib/kf-judge/captures/nested",
        ] {
            let mut c = config();
            if field == "tasks" {
                c.tasks_root = path.into();
            } else {
                c.image_root = path.into();
            }
            assert_eq!(c.validate().unwrap_err().code, "capture_lease_overlap");
        }
    }
}
