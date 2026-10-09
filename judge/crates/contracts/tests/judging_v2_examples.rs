use kf_contracts::judging_versions as versions;
use kf_contracts::{judging as v1, judging_v2 as v2};
use serde::{Serialize, de::DeserializeOwned};
use std::{fs, path::PathBuf};

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/contracts-v2")
}
fn fixture<T: DeserializeOwned>(name: &str) -> T {
    v1::decode_wire(&fs::read(fixtures().join(format!("{name}.json"))).unwrap()).unwrap()
}
fn roundtrip<T: DeserializeOwned + Serialize>(data: &[u8], invalid: bool, name: &str) {
    let result = v1::decode_wire::<T>(data);
    if invalid {
        assert!(result.is_err(), "must reject {name}");
    } else {
        assert_eq!(
            serde_json::to_value(result.unwrap_or_else(|e| panic!("{name}: {e}"))).unwrap(),
            serde_json::from_slice::<serde_json::Value>(data).unwrap(),
            "roundtrip {name}"
        );
    }
}
#[test]
fn shared_v2_acceptance_rejection_and_canonical_bytes_match_python() {
    let mut count = 0;
    for entry in fs::read_dir(fixtures()).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_none_or(|e| e != "json") {
            continue;
        }
        let name = path.file_name().unwrap().to_string_lossy();
        let data = fs::read(&path).unwrap();
        let invalid = name.contains(".invalid-");
        match name.split('.').next().unwrap() {
            "judge-manifest" => roundtrip::<v2::JobManifest>(&data, invalid, &name),
            "judge-lease" => roundtrip::<v2::LeaseGrant>(&data, invalid, &name),
            "judge-stage" => roundtrip::<v2::StageReport>(&data, invalid, &name),
            "judge-result" => roundtrip::<v2::JudgementReport>(&data, invalid, &name),
            "judge-admission" => roundtrip::<v2::AdmissionPolicy>(&data, invalid, &name),
            "judge-registry" => roundtrip::<v2::AdmissionRegistry>(&data, invalid, &name),
            "judge-stopped" => roundtrip::<v2::StoppedReport>(&data, invalid, &name),
            _ => panic!("unrecognized fixture {name}"),
        }
        count += 1;
    }
    assert!(count >= 53);
    for language in ["c17", "cpp20", "python313", "java21"] {
        let manifest: v2::JobManifest = fixture(&format!("judge-manifest.valid-{language}"));
        assert_eq!(
            v2::manifest_bytes(&manifest).unwrap(),
            fs::read(fixtures().join(format!("canonical-manifest.{language}.txt"))).unwrap()
        );
        let data =
            fs::read(fixtures().join(format!("judge-manifest.valid-{language}.json"))).unwrap();
        assert!(v1::decode_wire::<v1::JobManifest>(&data).is_err());
    }
}
#[test]
fn runtime_identity_admission_and_program_binding_are_exact() {
    let policy: v2::AdmissionPolicy = fixture("judge-admission.valid");
    for language in ["c17", "cpp20", "python313", "java21"] {
        let manifest: v2::JobManifest = fixture(&format!("judge-manifest.valid-{language}"));
        for attempt in [1, 2, 3] {
            policy.admit(&manifest, attempt).unwrap();
        }
        for attempt in [0, 4] {
            assert!(policy.admit(&manifest, attempt).is_err());
        }
        let mut wrong = manifest.clone();
        wrong.toolchain.image_revision += 1;
        assert!(policy.admit(&wrong, 1).is_err());
        wrong = manifest.clone();
        wrong.toolchain.image_digest = "f".repeat(64).try_into().unwrap();
        assert!(policy.admit(&wrong, 1).is_err());
        let digest: v1::Digest =
            fs::read_to_string(fixtures().join(format!("canonical-manifest.{language}.sha256")))
                .unwrap()
                .trim()
                .to_owned()
                .try_into()
                .unwrap();
        let result: v2::JudgementReport = fixture(&format!("judge-result.valid-{language}"));
        result.validate_for_manifest(&manifest, &digest).unwrap();
        let mut wrong = manifest.clone();
        wrong.source.bytes += 1;
        assert!(result.validate_for_manifest(&wrong, &digest).is_err());
        let stage: v2::StageReport = fixture(&format!("judge-stage.valid-{language}"));
        stage.validate_for_manifest(&manifest).unwrap();
    }
    let mut restricted = policy;
    restricted
        .capabilities
        .retain(|c| c.language != v2::Language::Python313);
    let manifest: v2::JobManifest = fixture("judge-manifest.valid-python313");
    assert!(restricted.admit(&manifest, 1).is_err());
}
#[test]
fn runtime_entry_profiles_and_jvm_nonheap_memory_are_fixed() {
    let python = v2::Language::Python313.contract();
    assert_eq!(python.source_name, "main.py");
    assert_eq!(python.preparation_profile, "python_check");
    assert_eq!(python.preparation_kind, v2::PreparationKind::SyntaxCheck);
    let java = v2::Language::Java21.contract();
    assert_eq!(java.source_name, "Main.java");
    assert_eq!(java.run_profile, "java_run");
    let manifest: v2::JobManifest = fixture("judge-manifest.valid-java21");
    let heap = v2::jvm_heap_bytes(&manifest.run_limits).unwrap();
    assert!(heap >= 32 * v1::MIB && heap < manifest.run_limits.memory_bytes);
    let mut more = manifest.run_limits.clone();
    more.processes = 32;
    assert!(v2::jvm_heap_bytes(&more).unwrap() < heap);
    more.memory_bytes = 128 * v1::MIB;
    assert!(v2::jvm_heap_bytes(&more).is_err());
}

#[test]
fn storage_boundary_retains_version_identity_and_rejects_mismatched_results() {
    let original = fixtures().parent().unwrap().join("contracts");
    let old_manifest: versions::JobManifest =
        v1::decode_wire(&fs::read(original.join("judge-manifest.valid.json")).unwrap()).unwrap();
    let old_report: versions::JudgementReport =
        v1::decode_wire(&fs::read(original.join("judge-result.valid.json")).unwrap()).unwrap();
    let manifest: versions::JobManifest = fixture("judge-manifest.valid-python313");
    let report: versions::JudgementReport = fixture("judge-result.valid-python313");
    let digest: v1::Digest = match &old_report {
        versions::JudgementReport::V1(r) => r.manifest_digest.clone(),
        _ => unreachable!(),
    };
    old_report
        .validate_for_manifest(&old_manifest, &digest)
        .unwrap();
    let new_digest: v1::Digest =
        fs::read_to_string(fixtures().join("canonical-manifest.python313.sha256"))
            .unwrap()
            .trim()
            .to_owned()
            .try_into()
            .unwrap();
    report
        .validate_for_manifest(&manifest, &new_digest)
        .unwrap();
    assert!(
        old_report
            .validate_for_manifest(&manifest, &digest)
            .is_err()
    );
    assert!(
        report
            .validate_for_manifest(&old_manifest, &digest)
            .is_err()
    );
    assert_eq!(
        old_manifest.canonical_bytes().unwrap(),
        fs::read(original.join("canonical-manifest.v1.txt")).unwrap()
    );
    let data = fs::read(fixtures().join("judge-manifest.valid-python313.json")).unwrap();
    let text = String::from_utf8(data).unwrap();
    for replacement in [
        "\"schema_version\": 3",
        "\"schema_version\": true",
        "\"schema_version\": 2, \"schema_version\": 2",
    ] {
        let invalid = text.replace("\"schema_version\": 2", replacement);
        assert!(v1::decode_wire::<versions::JobManifest>(invalid.as_bytes()).is_err());
    }
}

#[test]
fn registry_explicitly_admits_both_protocols_without_promoting_legacy_policy() {
    let registry: v2::AdmissionRegistry = fixture("judge-registry.valid");
    let original = fixtures().parent().unwrap().join("contracts");
    let old: versions::JobManifest =
        v1::decode_wire(&fs::read(original.join("judge-manifest.valid.json")).unwrap()).unwrap();
    registry.admit(&old, 1).unwrap();
    for language in ["c17", "cpp20", "python313", "java21"] {
        let manifest: versions::JobManifest = fixture(&format!("judge-manifest.valid-{language}"));
        registry.admit(&manifest, 1).unwrap();
        let mut restricted = registry.clone();
        restricted.multi_runtime = None;
        assert!(restricted.admit(&manifest, 1).is_err());
        restricted = registry.clone();
        restricted.legacy = None;
        assert!(restricted.admit(&old, 1).is_err());
    }
    let mut wrong = registry;
    wrong.multi_runtime.as_mut().unwrap().max_retries += 1;
    assert!(wrong.validate().is_err());
}
