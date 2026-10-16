import json
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from kf_oj.contracts.judge_state import (
    EvaluationState,
    require_current_lease,
    require_transition,
)
from kf_oj.contracts.judging import (
    MAX_WIRE_BYTES,
    WIRE_MODELS,
    AdmissionPolicy,
    JobManifest,
    JudgementReport,
    LeaseGrant,
    decode_wire,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts"
PATHS = sorted(p for name in WIRE_MODELS for p in FIXTURES.glob(f"{name}.*.json"))


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.stem)
def test_shared_judging_examples(path: Path) -> None:
    name = path.name.split(".")[0]
    model = WIRE_MODELS[name]
    payload = json.loads(path.read_text())
    schema = json.loads((ROOT / f"contracts/schemas/{name}.v1.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    if ".invalid-" in path.name:
        with pytest.raises(ValueError):
            decode_wire(model, path.read_bytes())
        if ".invalid-semantic-" in path.name:
            assert validator.is_valid(payload)
        else:
            assert not validator.is_valid(payload)
    else:
        decoded = decode_wire(model, path.read_bytes())
        validator.validate(payload)
        if name == "judge-admission" and "defaults" in path.name:
            assert decoded.max_concurrent_jobs == decoded.prefetch == 1
            assert decoded.max_retries == 2
        else:
            assert decoded.model_dump(mode="json") == payload


def fixture(name: str, model):
    return decode_wire(model, (FIXTURES / f"{name}.valid.json").read_bytes())


def test_every_state_transition_matches_shared_table() -> None:
    transitions = json.loads((FIXTURES / "judge-state.transitions.json").read_text())
    assert set(transitions) == {s.value for s in EvaluationState}
    for current in EvaluationState:
        for target in EvaluationState:
            allowed = target.value in transitions[current.value]
            assert current.allows(target) == allowed
            if allowed:
                require_transition(current, target)
            else:
                with pytest.raises(ValueError):
                    require_transition(current, target)


def test_lease_fencing_identity_and_expiry_boundary() -> None:
    grant = fixture("judge-lease", LeaseGrant)
    binding = grant.binding
    require_current_lease(binding, binding, grant.expires_at_unix_ms, grant.expires_at_unix_ms - 1)
    for now in (grant.expires_at_unix_ms, grant.expires_at_unix_ms + 1, -1):
        with pytest.raises(ValueError):
            require_current_lease(binding, binding, grant.expires_at_unix_ms, now)
    for field, value in (
        ("fencing_version", 2),
        ("attempt_id", UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000009")),
        ("worker_id", UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000009")),
        ("evaluation_id", UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000009")),
    ):
        supplied = binding.model_copy(update={field: value})
        with pytest.raises(ValueError):
            require_current_lease(binding, supplied, grant.expires_at_unix_ms, 1)
    assert grant.lease_token not in repr(grant)


def test_node_admission_never_accepts_job_or_retry_above_trusted_policy() -> None:
    manifest = fixture("judge-manifest", JobManifest)
    policy = fixture("judge-admission", AdmissionPolicy)
    for attempt in (1, 2, 3):
        policy.admit(manifest, attempt)
    for attempt in (0, 4, True):
        with pytest.raises(ValueError):
            policy.admit(manifest, attempt)
    for field, value in (("max_package_bytes", 1), ("max_job_wall_time_us", 1000)):
        with pytest.raises(ValueError):
            policy.model_copy(update={field: value}).admit(manifest, 1)
    lower = policy.run_ceiling.model_copy(update={"memory_bytes": 32 * 1024 * 1024})
    with pytest.raises(ValueError):
        policy.model_copy(update={"run_ceiling": lower}).admit(manifest, 1)
    other = manifest.model_copy(
        update={"toolchain": manifest.toolchain.model_copy(update={"image_revision": 2})}
    )
    with pytest.raises(ValueError):
        policy.admit(other, 1)


def test_result_requires_authorized_manifest_and_complete_case_order() -> None:
    manifest = fixture("judge-manifest", JobManifest)
    result = fixture("judge-result", JudgementReport)
    result.validate_for_manifest(manifest, result.manifest_digest)
    with pytest.raises(ValueError):
        result.validate_for_manifest(manifest, "f" * 64)
    short = result.model_dump(mode="json")
    short.update(cases=short["cases"][:1], run_cpu_time_us=100, run_memory_peak_bytes=256 * 1024)
    valid_partial = JudgementReport.model_validate(short)
    with pytest.raises(ValueError):
        valid_partial.validate_for_manifest(manifest, result.manifest_digest)
    wrong_order = result.model_dump(mode="json")
    wrong_order["cases"].reverse()
    with pytest.raises(ValueError):
        JudgementReport.model_validate(wrong_order).validate_for_manifest(
            manifest, result.manifest_digest
        )


def test_wire_decoding_bounds_ambiguity_and_private_error_output() -> None:
    payload = (FIXTURES / "judge-lease.valid.json").read_bytes()
    duplicate = payload.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    for invalid in (duplicate, payload.decode().encode("utf-16"), b" " * (MAX_WIRE_BYTES + 1)):
        with pytest.raises(ValueError):
            decode_wire(LeaseGrant, invalid)
    secret = "do-not-echo-private-token"
    with pytest.raises(ValueError) as error:
        decode_wire(
            LeaseGrant, payload.replace(b'"0' + b"0" * 63 + b'"', json.dumps(secret).encode())
        )
    assert secret not in str(error.value)
    with pytest.raises(ValidationError):
        LeaseGrant.model_validate({})
