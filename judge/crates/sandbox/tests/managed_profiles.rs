//! Ordinary lexical/authority policy tests. Actual runtime/kernel evidence is VM-only.
use kf_contracts::judging_v2::{ImageKind as ContractKind, RuntimeIdentity};
use kf_sandbox::{
    profiles,
    runtime::{ImageKind, RegisteredImage, RuntimeConfig},
    spec::*,
};

fn identity(profile: ExecutionProfile) -> RuntimeIdentity {
    let python = matches!(
        profile,
        ExecutionProfile::PythonCheck | ExecutionProfile::PythonRun
    );
    RuntimeIdentity {
        toolchain_id: (if python {
            "cpython313-v1"
        } else {
            "openjdk21-v1"
        })
        .to_owned()
        .try_into()
        .unwrap(),
        image_kind: if python {
            ContractKind::CPython313
        } else {
            ContractKind::OpenJdk21
        },
        image_revision: 1,
        image_digest: "a".repeat(64).try_into().unwrap(),
        runtime_version: (if python { "3.13.16" } else { "21.0.12.1+1-LTS" })
            .to_owned()
            .try_into()
            .unwrap(),
    }
}

fn registry() -> RuntimeConfig {
    let mut c: RuntimeConfig = serde_json::from_str(include_str!(
        "../../../../deploy/sandbox.runtime.example.json"
    ))
    .unwrap();
    for (profile, kind, root) in [
        (
            ExecutionProfile::PythonRun,
            ImageKind::LinuxCPython313X86_64V1,
            "python313",
        ),
        (
            ExecutionProfile::JavaRun,
            ImageKind::LinuxOpenJdk21X86_64V1,
            "java21",
        ),
    ] {
        c.runtime_images.push(RegisteredImage {
            image_root: format!("/var/lib/kf-judge/images/{root}"),
            image_kind: kind,
            runtime_identity: identity(profile),
            per_task_ceiling: c.per_task_ceiling.clone(),
        });
    }
    c
}

#[test]
fn image_selection_preserves_one_global_identity_pool_budget_and_legacy_wire() {
    let c = registry().validate().unwrap();
    for profile in [ExecutionProfile::PythonRun, ExecutionProfile::JavaRun] {
        let selected = c.for_request(&request(profile)).unwrap();
        let p = selected.config();
        assert_eq!(p.runtime_identity, Some(identity(profile)));
        assert_eq!(p.identities, c.config().identities);
        assert_eq!(p.aggregate, c.config().aggregate);
        assert_eq!(p.state_root, c.config().state_root);
        assert_eq!(p.tasks_root, c.config().tasks_root);
        assert_eq!(p.delegated_cgroup_root, c.config().delegated_cgroup_root);
        assert_eq!(p.max_concurrent_runs, c.config().max_concurrent_runs);
        assert!(p.runtime_images.is_empty());
    }
    let legacy: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    let selected = c.for_request(&legacy).unwrap();
    let original: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../deploy/sandbox.runtime.example.json"
    ))
    .unwrap();
    assert_eq!(serde_json::to_value(selected.config()).unwrap(), original);
}

#[test]
fn registry_cannot_grant_unregistered_identity_profile_or_wider_stage_budget() {
    let mut c = registry();
    c.runtime_images[0].per_task_ceiling.memory_bytes = 64 << 20;
    c.runtime_images[0].per_task_ceiling.stack_bytes = 8 << 20;
    let c = c.validate().unwrap();
    let good = request(ExecutionProfile::PythonRun);
    assert!(c.for_request(&good).is_ok());
    let mut wrong = good.clone();
    wrong.runtime_identity.as_mut().unwrap().image_digest = "b".repeat(64).try_into().unwrap();
    assert!(c.for_request(&wrong).is_err());
    let mut wrong = good.clone();
    wrong.runtime_identity = None;
    assert!(c.for_request(&wrong).is_err());
    let mut wrong = request(ExecutionProfile::JavaRun);
    wrong.runtime_identity = good.runtime_identity.clone();
    assert!(c.for_request(&wrong).is_err());
    let mut wrong = good;
    wrong.limits.memory_bytes = 128 << 20;
    assert!(c.for_request(&wrong).is_err());
}

#[test]
fn registry_rejects_overlapping_images_private_roots_duplicate_families_and_capacity() {
    for path in [
        "/tmp/python",
        "/var/lib/kf-judge/tasks/x",
        "/var/lib/kf-judge/identities/x",
        "/var/lib/kf-judge/captures/x",
        "/var/lib/kf-judge/factory/x",
        "/var/lib/kf-judge/images/java21",
        "/var/lib/kf-judge/images/java21/nested",
        "/var/lib/kf-judge/images",
        "/var/lib/kf-judge/images/../x",
    ] {
        let mut c = registry();
        c.runtime_images[0].image_root = path.into();
        assert!(c.validate().is_err(), "{path}");
    }
    let mut c = registry();
    c.runtime_images[1].runtime_identity = c.runtime_images[0].runtime_identity.clone();
    c.runtime_images[1].image_kind = c.runtime_images[0].image_kind;
    assert_eq!(c.validate().unwrap_err().code, "duplicate_runtime_family");
    for field in ["memory_bytes", "processes", "workdir_bytes"] {
        let mut value = serde_json::to_value(registry()).unwrap();
        let n = value["runtime_images"][0]["per_task_ceiling"][field]
            .as_u64()
            .unwrap();
        value["runtime_images"][0]["per_task_ceiling"][field] = (n + 1).into();
        let c: RuntimeConfig = serde_json::from_value(value).unwrap();
        assert_eq!(
            c.validate().unwrap_err().code,
            "ceiling_exceeds_global_budget"
        );
    }
}

fn request(profile: ExecutionProfile) -> ExecutionSpec {
    let limits = ResourceLimits::for_profile(profile);
    ExecutionSpec {
        schema_version: kf_contracts::SchemaVersion,
        runtime_identity: Some(identity(profile)),
        run_id: "managed-policy".into(),
        profile,
        argv: profiles::argv(profile, &limits).unwrap(),
        env: Default::default(),
        cwd: ".".into(),
        inputs: if profile == ExecutionProfile::JavaRun {
            [
                "Helper.class",
                "Main$1.class",
                "Main$Inner.class",
                "Main.class",
            ]
            .iter()
            .enumerate()
            .map(|(i, path)| InputArtifact {
                artifact_id: format!("class-{i}"),
                path: (*path).into(),
            })
            .collect()
        } else {
            vec![InputArtifact {
                artifact_id: "source".into(),
                path: if profile == ExecutionProfile::JavaCompile {
                    "Main.java"
                } else {
                    "main.py"
                }
                .into(),
            }]
        },
        stdin_artifact: None,
        outputs: vec![],
        limits,
    }
}

#[test]
fn managed_requests_bind_the_exact_operator_identity() {
    let legacy: RuntimeConfig = serde_json::from_str(include_str!(
        "../../../../deploy/sandbox.runtime.example.json"
    ))
    .unwrap();
    for (profile, kind) in [
        (
            ExecutionProfile::PythonRun,
            ImageKind::LinuxCPython313X86_64V1,
        ),
        (ExecutionProfile::JavaRun, ImageKind::LinuxOpenJdk21X86_64V1),
    ] {
        let mut config = legacy.clone();
        config.image_kind = kind;
        config.runtime_identity = Some(identity(profile));
        let policy = config.validate().unwrap().spec_policy();
        let original = request(profile);
        assert!(original.clone().validate(&policy).is_ok());
        let mut missing = original.clone();
        missing.runtime_identity = None;
        assert_eq!(
            missing.validate(&policy).unwrap_err().code,
            ValidationCode::InvalidRuntimeIdentity
        );
        for field in ["digest", "revision", "version", "kind", "toolchain"] {
            let mut wrong = original.clone();
            let id = wrong.runtime_identity.as_mut().unwrap();
            match field {
                "digest" => id.image_digest = "b".repeat(64).try_into().unwrap(),
                "revision" => id.image_revision += 1,
                "version" => id.runtime_version = "21.0.99+1-LTS".to_owned().try_into().unwrap(),
                "kind" => id.image_kind = ContractKind::GnuCxx,
                "toolchain" => id.toolchain_id = "gnu-cxx-v1".to_owned().try_into().unwrap(),
                _ => unreachable!(),
            }
            assert_eq!(
                wrong.validate(&policy).unwrap_err().code,
                ValidationCode::InvalidRuntimeIdentity,
                "{field}"
            );
        }
    }
    let request: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    assert!(request.runtime_identity.is_none());
    assert!(
        !serde_json::to_string(&request)
            .unwrap()
            .contains("runtime_identity")
    );
    assert!(
        request
            .clone()
            .validate(&legacy.validate().unwrap().spec_policy())
            .is_ok()
    );
    let mut wrong = request;
    wrong.runtime_identity = Some(identity(ExecutionProfile::PythonRun));
    assert!(wrong.validate(&SpecPolicy::default()).is_err());
}

#[test]
fn commands_environment_and_source_or_class_sets_are_closed() {
    for profile in [
        ExecutionProfile::PythonCheck,
        ExecutionProfile::PythonRun,
        ExecutionProfile::JavaCompile,
        ExecutionProfile::JavaRun,
    ] {
        let original = request(profile);
        let valid = original.clone().validate(&SpecPolicy::default()).unwrap();
        assert_eq!(valid.execution_environment()["LANG"], "C.UTF-8");
        assert!(!valid.execution_environment().contains_key("PATH"));
        for arg in [
            "-c",
            "-m",
            "-javaagent:/work/user.jar",
            "-cp",
            "@options",
            "/bin/sh",
        ] {
            let mut modified = original.clone();
            modified.argv.push(arg.into());
            assert!(modified.validate(&SpecPolicy::default()).is_err());
        }
        for variable in [
            "PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "JAVA_TOOL_OPTIONS",
            "JDK_JAVA_OPTIONS",
            "CLASSPATH",
            "LD_PRELOAD",
            "LANG",
        ] {
            let mut modified = original.clone();
            modified.env.insert(variable.into(), "C".into());
            assert!(modified.validate(&SpecPolicy::default()).is_err());
        }
        for input in [
            "../Main.class",
            "pkg/Main.class",
            "user.jar",
            "Main$1.class/child",
            "main.pyc",
            "options",
        ] {
            let mut modified = original.clone();
            modified.inputs[0].path = input.into();
            assert!(modified.validate(&SpecPolicy::default()).is_err());
        }
        let mut changed = original.clone();
        changed.cwd = "tmp".into();
        assert!(changed.validate(&SpecPolicy::default()).is_err());
        let mut changed = original;
        changed.outputs.push("user.jar".into());
        assert!(changed.validate(&SpecPolicy::default()).is_err());
    }
    let mut missing = request(ExecutionProfile::JavaRun);
    missing.inputs.pop();
    assert!(missing.validate(&SpecPolicy::default()).is_err());
    let mut duplicate = request(ExecutionProfile::JavaRun);
    duplicate.inputs.push(duplicate.inputs[0].clone());
    assert!(duplicate.validate(&SpecPolicy::default()).is_err());
}

#[test]
fn startup_heap_and_budget_parameters_cannot_be_supplied_by_request() {
    for profile in [ExecutionProfile::JavaCompile, ExecutionProfile::JavaRun] {
        let original = request(profile);
        assert!(
            original
                .argv
                .iter()
                .any(|a| a.contains("ActiveProcessorCount=1"))
        );
        assert!(
            original
                .argv
                .iter()
                .any(|a| a.contains("DisableAttachMechanism"))
        );
        assert!(original.argv.iter().any(|a| a.contains("UseSerialGC")));
        for property in ["stdout.encoding", "stderr.encoding"] {
            let expected = format!(
                "{}-D{property}=UTF-8",
                if profile == ExecutionProfile::JavaCompile {
                    "-J"
                } else {
                    ""
                }
            );
            assert!(original.argv.contains(&expected));
            let mut changed = original.clone();
            let index = changed.argv.iter().position(|a| a == &expected).unwrap();
            changed.argv[index] = expected.replace("UTF-8", "US-ASCII");
            assert!(changed.clone().validate(&SpecPolicy::default()).is_err());
            changed.argv.remove(index);
            assert!(changed.validate(&SpecPolicy::default()).is_err());
        }
        let mut changed = original.clone();
        changed.limits.memory_bytes += 1024 * 1024;
        assert!(changed.validate(&SpecPolicy::default()).is_err());
        let mut tiny = original.limits;
        tiny.memory_bytes = 128 * 1024 * 1024;
        assert!(profiles::argv(profile, &tiny).is_err());
    }
    let mut small = request(ExecutionProfile::PythonRun);
    small.limits.open_files = 8;
    assert!(small.validate(&SpecPolicy::default()).is_err());
}

#[test]
fn root_image_kind_never_promotes_legacy_authority() {
    let legacy: RuntimeConfig = serde_json::from_str(include_str!(
        "../../../../deploy/sandbox.runtime.example.json"
    ))
    .unwrap();
    let old = legacy.clone().validate().unwrap();
    assert!(
        request(ExecutionProfile::PythonRun)
            .validate(&old.spec_policy())
            .is_err()
    );
    for (kind, contract, profile, id, version) in [
        (
            ImageKind::LinuxCPython313X86_64V1,
            ContractKind::CPython313,
            ExecutionProfile::PythonRun,
            "cpython313-v1",
            "3.13.16",
        ),
        (
            ImageKind::LinuxOpenJdk21X86_64V1,
            ContractKind::OpenJdk21,
            ExecutionProfile::JavaRun,
            "openjdk21-v1",
            "21.0.12.1+1-LTS",
        ),
    ] {
        let mut config = legacy.clone();
        config.image_kind = kind;
        assert!(config.clone().validate().is_err());
        config.runtime_identity = Some(RuntimeIdentity {
            toolchain_id: id.to_owned().try_into().unwrap(),
            image_kind: contract,
            image_revision: 1,
            image_digest: "a".repeat(64).try_into().unwrap(),
            runtime_version: version.to_owned().try_into().unwrap(),
        });
        let validated = config.clone().validate().unwrap();
        assert!(request(profile).validate(&validated.spec_policy()).is_ok());
        assert!(
            request(if profile == ExecutionProfile::PythonRun {
                ExecutionProfile::JavaRun
            } else {
                ExecutionProfile::PythonRun
            })
            .validate(&validated.spec_policy())
            .is_err()
        );
        config.runtime_identity.as_mut().unwrap().image_revision = 0;
        assert!(config.validate().is_err());
    }
}
