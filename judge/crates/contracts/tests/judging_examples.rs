use kf_contracts::{judging::*, state::*};
use serde::de::DeserializeOwned;
use serde_json::Value;
use std::{fs, path::PathBuf};

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/contracts")
}
fn fixture<T: DeserializeOwned>(name: &str) -> T {
    decode_wire(&fs::read(fixtures().join(format!("{name}.valid.json"))).unwrap()).unwrap()
}

#[test]
fn canonical_manifest_bytes_match_python_snapshot() {
    let manifest: JobManifest = fixture("judge-manifest");
    assert_eq!(
        manifest_bytes(&manifest).unwrap(),
        fs::read(fixtures().join("canonical-manifest.v1.txt")).unwrap()
    );
}
fn roundtrip<T: DeserializeOwned + serde::Serialize>(bytes: &[u8], invalid: bool, name: &str) {
    let result = decode_wire::<T>(bytes);
    if invalid {
        assert!(result.is_err(), "must reject {name}");
    } else {
        let decoded =
            serde_json::to_value(result.unwrap_or_else(|e| panic!("{name}: {e}"))).unwrap();
        let mut original: Value = serde_json::from_slice(bytes).unwrap();
        if name.contains("admission") && name.contains("defaults") {
            let object = original.as_object_mut().unwrap();
            object.insert("max_concurrent_jobs".into(), 1.into());
            object.insert("prefetch".into(), 1.into());
            object.insert("max_retries".into(), 2.into());
        }
        assert_eq!(decoded, original, "roundtrip {name}");
    }
}
#[test]
fn shared_judging_examples_match_python() {
    let mut count = 0;
    for entry in fs::read_dir(fixtures()).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy();
        let bytes = fs::read(&path).unwrap();
        let invalid = name.contains(".invalid-");
        if name.starts_with("judge-manifest.") {
            roundtrip::<JobManifest>(&bytes, invalid, &name);
        } else if name.starts_with("judge-lease.") {
            roundtrip::<LeaseGrant>(&bytes, invalid, &name);
        } else if name.starts_with("judge-stage.") {
            roundtrip::<StageReport>(&bytes, invalid, &name);
        } else if name.starts_with("judge-result.") {
            roundtrip::<JudgementReport>(&bytes, invalid, &name);
        } else if name.starts_with("judge-admission.") {
            roundtrip::<AdmissionPolicy>(&bytes, invalid, &name);
        } else {
            continue;
        }
        count += 1;
    }
    assert!(
        count >= 50,
        "judging acceptance/rejection cases must not disappear"
    );
}
#[test]
fn every_state_transition_matches_shared_table() {
    let table: Value =
        serde_json::from_slice(&fs::read(fixtures().join("judge-state.transitions.json")).unwrap())
            .unwrap();
    let states = [
        EvaluationState::Queued,
        EvaluationState::Claimed,
        EvaluationState::Compiling,
        EvaluationState::Running,
        EvaluationState::Cancelling,
        EvaluationState::Finished,
        EvaluationState::Cancelled,
        EvaluationState::Failed,
    ];
    assert_eq!(table.as_object().unwrap().len(), states.len());
    for current in states {
        for target in states {
            let name = serde_json::to_value(current).unwrap();
            let allowed = table[name.as_str().unwrap()]
                .as_array()
                .unwrap()
                .contains(&serde_json::to_value(target).unwrap());
            assert_eq!(current.allows(target), allowed);
            assert_eq!(require_transition(current, target).is_ok(), allowed);
        }
    }
}
#[test]
fn lease_fencing_identity_and_deadline_are_strict() {
    let grant: LeaseGrant = fixture("judge-lease");
    let binding = &grant.binding;
    assert!(
        require_current_lease(
            binding,
            binding,
            grant.expires_at_unix_ms,
            grant.expires_at_unix_ms - 1
        )
        .is_ok()
    );
    for now in [grant.expires_at_unix_ms, grant.expires_at_unix_ms + 1] {
        assert!(require_current_lease(binding, binding, grant.expires_at_unix_ms, now).is_err());
    }
    let foreign: Identifier = "aaaaaaaa-aaaa-4aaa-8aaa-000000000009"
        .to_owned()
        .try_into()
        .unwrap();
    for i in 0..4 {
        let mut supplied = binding.clone();
        match i {
            0 => supplied.fencing_version += 1,
            1 => supplied.attempt_id = foreign,
            2 => supplied.worker_id = foreign,
            _ => supplied.evaluation_id = foreign,
        }
        assert!(require_current_lease(binding, &supplied, grant.expires_at_unix_ms, 1).is_err());
    }
    assert!(!format!("{grant:?}").contains(grant.lease_token.expose_secret()));
}
#[test]
fn trusted_admission_caps_and_retry_limit_cannot_be_relaxed_by_job() {
    let manifest: JobManifest = fixture("judge-manifest");
    let policy: AdmissionPolicy = fixture("judge-admission");
    for attempt in [1, 2, 3] {
        assert!(policy.admit(&manifest, attempt).is_ok());
    }
    for attempt in [0, 4] {
        assert!(policy.admit(&manifest, attempt).is_err());
    }
    let mut other = manifest.clone();
    other.toolchain.image_revision += 1;
    assert!(policy.admit(&other, 1).is_err());
    let mut lower = policy.clone();
    lower.max_package_bytes = 1;
    assert!(lower.admit(&manifest, 1).is_err());
    let mut lower = policy.clone();
    lower.max_job_wall_time_us = 1000;
    assert!(lower.admit(&manifest, 1).is_err());
    let mut lower = policy;
    lower.run_ceiling.memory_bytes = 32 * MIB;
    assert!(lower.admit(&manifest, 1).is_err());
}
#[test]
fn results_require_authorized_manifest_and_complete_case_order() {
    let manifest: JobManifest = fixture("judge-manifest");
    let result: JudgementReport = fixture("judge-result");
    assert!(
        result
            .validate_for_manifest(&manifest, &result.manifest_digest)
            .is_ok()
    );
    assert!(
        result
            .validate_for_manifest(&manifest, &"f".repeat(64).try_into().unwrap())
            .is_err()
    );
    let mut short = result.clone();
    short.cases.truncate(1);
    short.run_cpu_time_us = Some(100);
    short.run_memory_peak_bytes = Some(256 * 1024);
    assert!(short.validate().is_ok());
    assert!(
        short
            .validate_for_manifest(&manifest, &result.manifest_digest)
            .is_err()
    );
    let mut wrong_order = result;
    wrong_order.cases.reverse();
    assert!(wrong_order.validate().is_ok());
    assert!(
        wrong_order
            .validate_for_manifest(&manifest, &wrong_order.manifest_digest)
            .is_err()
    );
}
#[test]
fn wire_decoder_bounds_ambiguity_and_private_diagnostics() {
    let bytes = fs::read(fixtures().join("judge-lease.valid.json")).unwrap();
    let text = String::from_utf8(bytes).unwrap();
    let duplicate = text.replace(
        "\"schema_version\": 1",
        "\"schema_version\": 1, \"schema_version\": 1",
    );
    assert!(decode_wire::<LeaseGrant>(duplicate.as_bytes()).is_err());
    assert!(decode_wire::<LeaseGrant>(&vec![b' '; MAX_WIRE_BYTES + 1]).is_err());
    let private = text.replace(&"0".repeat(64), "do-not-echo-private-token");
    let error = decode_wire::<LeaseGrant>(private.as_bytes()).unwrap_err();
    assert!(!error.contains("do-not-echo"));
    let ce: JudgementReport =
        decode_wire(&fs::read(fixtures().join("judge-result.valid-ce.json")).unwrap()).unwrap();
    assert!(!format!("{ce:?}").contains(ce.compilation.diagnostics.as_str()));
}
