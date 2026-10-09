//! Ordinary lexical/authority policy tests. Actual runtime/kernel evidence is VM-only.
use kf_contracts::judging_v2::{ImageKind as ContractKind, RuntimeIdentity};
use kf_sandbox::{
    profiles,
    runtime::{ImageKind, RuntimeConfig},
    spec::*,
};

fn request(profile: ExecutionProfile) -> ExecutionSpec {
    let limits = ResourceLimits::for_profile(profile);
    ExecutionSpec {
        schema_version: kf_contracts::SchemaVersion,
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
