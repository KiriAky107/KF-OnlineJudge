use kf_sandbox::spec::{
    ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits, SpecPolicy, ValidationCode,
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap()
}

fn spec() -> ExecutionSpec {
    serde_json::from_value(fixture()).unwrap()
}

fn rejected(request: ExecutionSpec, field: &str, code: ValidationCode) {
    let error = request.validate(&SpecPolicy::default()).unwrap_err();
    assert_eq!(error.field, field);
    assert_eq!(error.code, code);
}

#[test]
fn native_fixture_roundtrips_and_defaults_are_explicit() {
    let request = spec().validate(&SpecPolicy::default()).unwrap();
    assert_eq!(serde_json::to_value(request.spec()).unwrap(), fixture());
    assert_eq!(request.spec().limits, ResourceLimits::default());
}

#[test]
fn compiler_profiles_require_fixed_executables_and_accept_profile_defaults() {
    for (profile, executable) in [
        (ExecutionProfile::CompileC, "/usr/bin/gcc"),
        (ExecutionProfile::CompileCpp, "/usr/bin/g++"),
    ] {
        let mut request = spec();
        request.profile = profile;
        request.argv = vec![
            executable.into(),
            "source/main.c".into(),
            "-o".into(),
            "main".into(),
        ];
        request.inputs[0].path = "source/main.c".into();
        request.outputs = vec!["main".into()];
        request.limits = ResourceLimits::for_profile(profile);
        request.validate(&SpecPolicy::default()).unwrap();
        let mut invalid = spec();
        invalid.profile = profile;
        rejected(invalid, "argv[0]", ValidationCode::UnsupportedExecutable);
    }
}

#[test]
fn json_rejects_unknown_profiles_versions_host_mounts_and_missing_limits() {
    for (key, value) in [
        ("profile", json!("shell")),
        ("schema_version", json!(2)),
        ("mounts", json!([{"source": "/etc", "target": "/work"}])),
    ] {
        let mut document = fixture();
        document[key] = value;
        assert!(
            serde_json::from_value::<ExecutionSpec>(document).is_err(),
            "{key}"
        );
    }
    let mut document = fixture();
    document["limits"]
        .as_object_mut()
        .unwrap()
        .remove("memory_bytes");
    assert!(serde_json::from_value::<ExecutionSpec>(document).is_err());
    let mut document = fixture();
    document["inputs"][0]["writable"] = json!(true);
    assert!(serde_json::from_value::<ExecutionSpec>(document).is_err());
}

#[test]
fn argument_limits_reject_nul_empty_commands_and_excessive_size() {
    for argv in [
        vec![],
        vec!["/work/main".into(), "a\0b".into()],
        vec!["/work/main".into(), "x".repeat(4097)],
        vec!["x".into(); 129],
        std::iter::once("/work/main".into())
            .chain(std::iter::repeat_n("x".repeat(4096), 8))
            .collect(),
    ] {
        let mut request = spec();
        request.argv = argv;
        rejected(request, "argv", ValidationCode::InvalidArgv);
    }
    let mut request = spec();
    request.argv.push("参数 with spaces; $(not-a-shell)".into());
    request.validate(&SpecPolicy::default()).unwrap();
}

#[test]
fn native_executable_must_be_a_bound_work_artifact() {
    for executable in [
        "/bin/sh",
        "/work/../bin/sh",
        "/work/missing",
        "/work//main",
        "main",
        "",
    ] {
        let mut request = spec();
        request.argv[0] = executable.into();
        rejected(request, "argv[0]", ValidationCode::UnsupportedExecutable);
    }
}

#[test]
fn environment_does_not_accept_loader_overrides_or_arbitrary_values() {
    for (key, value) in [
        ("LD_PRELOAD", "lib.so"),
        ("PATH", "/host/bin"),
        ("TMPDIR", "/host/tmp"),
        ("LANG", "a\0b"),
        ("TZ", "Europe/London"),
    ] {
        let mut request = spec();
        request.env.insert(key.into(), value.into());
        rejected(request, "env", ValidationCode::InvalidEnvironment);
    }
}

#[test]
fn cleared_execution_environment_uses_only_profile_image_defaults() {
    let native = spec()
        .validate(&SpecPolicy::default())
        .unwrap()
        .execution_environment();
    assert!(!native.contains_key("PATH"));
    assert_eq!(native.get("TMPDIR").unwrap(), "/tmp");
    assert_eq!(native.get("TZ").unwrap(), "UTC");
    assert!(
        native
            .keys()
            .all(|key| ["LANG", "LC_ALL", "TZ", "TMPDIR"].contains(&key.as_str()))
    );
    for (profile, binary) in [
        (ExecutionProfile::CompileC, "/usr/bin/gcc"),
        (ExecutionProfile::CompileCpp, "/usr/bin/g++"),
    ] {
        let mut request = spec();
        request.profile = profile;
        request.argv[0] = binary.into();
        let env = request
            .validate(&SpecPolicy::default())
            .unwrap()
            .execution_environment();
        assert_eq!(env.get("PATH").unwrap(), "/usr/bin");
        assert_eq!(env.get("TMPDIR").unwrap(), "/tmp");
        assert!(!env.contains_key("HOME") && !env.contains_key("LD_LIBRARY_PATH"));
    }
}

#[test]
fn paths_reject_traversal_absolute_windows_ambiguous_and_long_components() {
    let mut invalid_paths = vec![
        "".into(),
        "/etc/passwd".into(),
        "../secret".into(),
        "a/../b".into(),
        "a//b".into(),
        "a/./b".into(),
        "a/".into(),
        "C:\\secret".into(),
        "%2e%2e/secret".into(),
        "a\0b".into(),
    ];
    invalid_paths.push("x".repeat(256));
    for path in invalid_paths {
        let mut request = spec();
        request.inputs[0].path = path;
        rejected(request, "inputs.path", ValidationCode::UnsafePath);
    }
    let mut request = spec();
    request.cwd = "../host".into();
    rejected(request, "cwd", ValidationCode::UnsafePath);
}

#[test]
fn identifiers_cannot_be_host_paths_or_empty_and_artifact_counts_are_bounded() {
    for value in ["", "../other", "/host/input", "a.b", "a\0b"] {
        let mut request = spec();
        request.run_id = value.into();
        rejected(request, "run_id", ValidationCode::InvalidIdentifier);
        let mut request = spec();
        request.stdin_artifact = Some(value.into());
        rejected(request, "stdin_artifact", ValidationCode::InvalidIdentifier);
    }
    let mut request = spec();
    request.inputs = vec![request.inputs[0].clone(); 65];
    rejected(request, "artifacts", ValidationCode::TooManyArtifacts);
    let mut request = spec();
    request.outputs = vec!["output".into(); 17];
    rejected(request, "artifacts", ValidationCode::TooManyArtifacts);
}

#[test]
fn conflicting_bindings_and_exports_cannot_replace_read_only_inputs() {
    for path in ["main", "main/child"] {
        let mut request = spec();
        request.inputs.push(InputArtifact {
            artifact_id: "other".into(),
            path: path.into(),
        });
        rejected(request, "inputs.path", ValidationCode::ConflictingPath);
    }
    let mut request = spec();
    request.outputs.push("main".into());
    rejected(request, "outputs", ValidationCode::ConflictingPath);
    let mut request = spec();
    request.outputs = vec!["out".into(), "out/file".into()];
    rejected(request, "outputs", ValidationCode::ConflictingPath);
}

#[test]
fn cwd_can_contain_an_input_but_cannot_be_or_descend_from_an_input_file() {
    let mut request = spec();
    request.inputs[0].path = "bin/main".into();
    request.argv[0] = "/work/bin/main".into();
    request.cwd = "bin".into();
    request.validate(&SpecPolicy::default()).unwrap();
    for cwd in ["main", "main/subdirectory"] {
        let mut request = spec();
        request.cwd = cwd.into();
        rejected(request, "inputs.path", ValidationCode::ConflictingPath);
    }
}

#[test]
fn every_resource_rejects_zero_and_exceeding_its_policy_ceiling() {
    for field in [
        "cpu_time_us",
        "wall_time_us",
        "memory_bytes",
        "stack_bytes",
        "processes",
        "open_files",
        "stdout_bytes",
        "stderr_bytes",
        "file_bytes",
        "workdir_bytes",
    ] {
        let mut document = fixture();
        document["limits"][field] = json!(0);
        let request = serde_json::from_value(document).unwrap();
        rejected(request, field, ValidationCode::InvalidLimit);
        let mut document = fixture();
        document["limits"][field] = json!(u32::MAX);
        let request = serde_json::from_value(document).unwrap();
        rejected(request, field, ValidationCode::LimitExceedsPolicy);
    }
    let mut document = fixture();
    document["limits"]["memory_bytes"] = json!(u64::MAX);
    rejected(
        serde_json::from_value(document).unwrap(),
        "memory_bytes",
        ValidationCode::LimitExceedsPolicy,
    );
    let mut document = fixture();
    document["limits"]["processes"] = json!(u64::MAX);
    assert!(serde_json::from_value::<ExecutionSpec>(document).is_err());
}

#[test]
fn trusted_policy_can_tighten_limits_and_rejects_inconsistent_caps() {
    let limits = ResourceLimits::default();
    let policy = SpecPolicy::with_limits_ceiling(limits.clone()).unwrap();
    let mut request = spec();
    request.limits.cpu_time_us += 1;
    assert_eq!(
        request.validate(&policy).unwrap_err().code,
        ValidationCode::LimitExceedsPolicy
    );
    let mut bad = limits;
    bad.stack_bytes = bad.memory_bytes + 1;
    assert_eq!(
        SpecPolicy::with_limits_ceiling(bad).unwrap_err().code,
        ValidationCode::InconsistentLimits
    );
    let mut request = spec();
    request.limits.file_bytes = request.limits.workdir_bytes + 1;
    rejected(request, "file_bytes", ValidationCode::LimitExceedsPolicy);
    let mut request = spec();
    request.limits.workdir_bytes = request.limits.file_bytes - 1;
    rejected(request, "limits", ValidationCode::InconsistentLimits);
}
