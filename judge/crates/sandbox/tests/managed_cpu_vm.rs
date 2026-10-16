#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
//! Run in a separate guest with AVX2 masked, using the ordinary Root binary.
#[allow(dead_code)]
mod support;
use kf_contracts::judging_v2::RuntimeIdentity;
use kf_sandbox::{
    image::{ImageCatalog, catalog_digest},
    profiles,
    runtime::ImageKind,
    spec::{ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits},
};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, process::Command};

#[test]
#[ignore = "independent capped VM, actual CPU lacks AVX2; no host runtime execution"]
fn insufficient_actual_cpu_refuses_before_image_execution() {
    support::require_vm();
    assert!(!std::is_x86_feature_detected!("avx2"));
    let dir = PathBuf::from(format!("/kf/cpu-refusal-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    for name in ["inputs", "outputs"] {
        fs::create_dir(dir.join(name)).unwrap();
    }
    let mut root = support::Root::new();
    root.config.max_concurrent_runs = 1;
    root.config.identities.count = 1;
    root.config.image_kind = ImageKind::LinuxCPython313X86_64V1;
    root.config.image_root = "/var/lib/kf-judge/images/python313".into();
    let data = fs::read(format!("{}/manifest.json", root.config.image_root)).unwrap();
    let catalog: ImageCatalog = serde_json::from_slice(&data).unwrap();
    assert_eq!(catalog.architecture, "x86_64_v3");
    let identity = RuntimeIdentity {
        toolchain_id: catalog.toolchain_id.clone(),
        image_kind: catalog.image_kind,
        image_revision: catalog.image_revision,
        image_digest: catalog_digest(&data),
        runtime_version: catalog.runtime_version.clone(),
    };
    catalog.validate(&identity).unwrap();
    root.config.runtime_identity = Some(identity.clone());
    root.config.clone().validate().unwrap();
    let limits = ResourceLimits::for_profile(ExecutionProfile::PythonRun);
    let request = ExecutionSpec {
        schema_version: kf_contracts::SchemaVersion,
        runtime_identity: Some(identity),
        run_id: "cpu-unsupported".into(),
        profile: ExecutionProfile::PythonRun,
        argv: profiles::argv(ExecutionProfile::PythonRun, &limits).unwrap(),
        env: Default::default(),
        cwd: ".".into(),
        inputs: vec![InputArtifact {
            artifact_id: "source".into(),
            path: "main.py".into(),
        }],
        stdin_artifact: None,
        outputs: vec![],
        limits,
    };
    request
        .clone()
        .validate(&root.validated().spec_policy())
        .unwrap();
    for (name, bytes) in [
        ("runtime.json", serde_json::to_vec(&root.config).unwrap()),
        ("spec.json", serde_json::to_vec(&request).unwrap()),
        (
            "inputs/source",
            b"raise AssertionError('incompatible runtime executed')\n".to_vec(),
        ),
    ] {
        fs::write(dir.join(name), bytes).unwrap();
        fs::set_permissions(dir.join(name), fs::Permissions::from_mode(0o600)).unwrap();
    }
    let output = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
        .env_clear()
        .args(["run", "--config"])
        .arg(dir.join("runtime.json"))
        .arg("--spec")
        .arg(dir.join("spec.json"))
        .arg("--artifacts")
        .arg(dir.join("inputs"))
        .arg("--output")
        .arg(dir.join("outputs"))
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let refused: serde_json::Value = serde_json::from_slice(&output.stderr).unwrap();
    assert_eq!(refused["error"], "runtime_not_ready");
    assert!(refused["cleanup_failure"].is_null());
    assert_eq!(fs::read_dir(dir.join("outputs")).unwrap().count(), 0);
    for name in ["tasks", "identities", "captures"] {
        let path = PathBuf::from("/var/lib/kf-judge").join(name);
        if path.exists() {
            assert_eq!(fs::read_dir(path).unwrap().count(), 0);
        }
    }
    assert!(
        !fs::read_dir(&root.path).unwrap().any(|entry| entry
            .unwrap()
            .file_type()
            .unwrap()
            .is_dir())
    );
    println!(
        "KF_ACTUAL_CPU_REFUSAL_PROOF {}",
        serde_json::json!({"actual_avx2":false,"image_architecture":catalog.architecture,"identity":request.runtime_identity,"refusal":refused,"publication_empty":true,"owned_resources_zero":true})
    );
    fs::remove_dir_all(dir).unwrap();
}
