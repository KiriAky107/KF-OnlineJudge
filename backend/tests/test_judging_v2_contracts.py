import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts import judging_versions as versions

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts-v2"
PATHS = sorted(FIXTURES.glob("*.json"))


def fixture(name, model):
    return v1.decode_wire(model, (FIXTURES / f"{name}.json").read_bytes())


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.stem)
def test_multi_runtime_examples_match_schema_and_rust(path):
    name = path.name.split(".")[0]
    model = v2.WIRE_MODELS[name]
    schema = json.loads((ROOT / f"contracts/schemas/{name}.v2.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    original = json.loads(path.read_text())
    if ".invalid-" in path.name:
        with pytest.raises(ValueError):
            v1.decode_wire(model, path.read_bytes())
        if ".invalid-semantic-" in path.name:
            assert validator.is_valid(original)
        else:
            assert not validator.is_valid(original)
    else:
        value = v1.decode_wire(model, path.read_bytes())
        validator.validate(original)
        assert value.model_dump(mode="json") == original


@pytest.mark.parametrize("language", list(v2.Language))
def test_explicit_version_keeps_old_consumers_closed_and_canonical_bytes_stable(language):
    path = FIXTURES / f"judge-manifest.valid-{language}.json"
    manifest = v1.decode_wire(v2.JobManifest, path.read_bytes())
    canonical = (FIXTURES / f"canonical-manifest.{language}.txt").read_bytes()
    assert v1.manifest_bytes(manifest) == canonical
    assert (
        hashlib.sha256(v1.manifest_bytes(manifest)).digest() == hashlib.sha256(canonical).digest()
    )
    with pytest.raises(ValueError):
        v1.decode_wire(v1.JobManifest, path.read_bytes())
    old = ROOT / "tests/fixtures/contracts/judge-manifest.valid.json"
    with pytest.raises(ValueError):
        v1.decode_wire(v2.JobManifest, old.read_bytes())
    old_manifest = v1.decode_wire(v1.JobManifest, old.read_bytes())
    assert (
        v1.manifest_bytes(old_manifest)
        == (ROOT / "tests/fixtures/contracts/canonical-manifest.v1.txt").read_bytes()
    )


def test_shared_capabilities_refuse_absent_changed_runtime_and_over_budget():
    policy = fixture("judge-admission.valid", v2.AdmissionPolicy)
    for language in v2.Language:
        manifest = fixture(f"judge-manifest.valid-{language}", v2.JobManifest)
        for attempt in (1, 2, 3):
            policy.admit(manifest, attempt)
        for attempt in (0, 4, True):
            with pytest.raises(ValueError):
                policy.admit(manifest, attempt)
        for field, value in (("image_revision", 2), ("image_digest", "f" * 64)):
            other = manifest.model_copy(
                update={"toolchain": manifest.toolchain.model_copy(update={field: value})}
            )
            with pytest.raises(ValueError):
                policy.admit(other, 1)
    python = fixture("judge-manifest.valid-python313", v2.JobManifest)
    without_python = policy.model_copy(
        update={
            "capabilities": tuple(c for c in policy.capabilities if c.language != python.language)
        }
    )
    with pytest.raises(ValueError):
        without_python.admit(python, 1)
    too_small = python.model_copy(
        update={"run_limits": python.run_limits.model_copy(update={"memory_bytes": 1})}
    )
    with pytest.raises(ValueError):
        policy.admit(too_small, 1)
    too_large = python.model_copy(
        update={"run_limits": python.run_limits.model_copy(update={"memory_bytes": 128 * v1.MIB})}
    )
    with pytest.raises(ValueError):
        policy.admit(too_large, 1)


@pytest.mark.parametrize("language", list(v2.Language))
def test_results_bind_exact_source_runtime_attempt_and_case_order(language):
    manifest = fixture(f"judge-manifest.valid-{language}", v2.JobManifest)
    result = fixture(f"judge-result.valid-{language}", v2.JudgementReport)
    digest = hashlib.sha256(versions.manifest_bytes(manifest)).hexdigest()
    assert digest == (FIXTURES / f"canonical-manifest.{language}.sha256").read_text().strip()
    result.validate_for_manifest(manifest, digest)
    for field, value in (("digest", "f" * 64), ("bytes", manifest.source.bytes + 1)):
        other = manifest.model_copy(
            update={"source": manifest.source.model_copy(update={field: value})}
        )
        with pytest.raises(ValueError):
            result.validate_for_manifest(other, digest)
    with pytest.raises(ValueError):
        result.validate_for_manifest(manifest, "f" * 64)
    changed_runtime = manifest.model_copy(
        update={"toolchain": manifest.toolchain.model_copy(update={"image_digest": "e" * 64})}
    )
    with pytest.raises(ValueError):
        result.validate_for_manifest(changed_runtime, digest)
    short = result.model_dump(mode="json")
    short.update(cases=short["cases"][:1], run_cpu_time_us=100, run_memory_peak_bytes=256 * 1024)
    valid_partial = v2.JudgementReport.model_validate(short)
    with pytest.raises(ValueError):
        valid_partial.validate_for_manifest(manifest, digest)


def test_python_check_is_not_an_accepted_result_and_stage_kind_is_bound():
    manifest = fixture("judge-manifest.valid-python313", v2.JobManifest)
    stage = fixture("judge-stage.valid-python313", v2.StageReport)
    stage.validate_for_manifest(manifest)
    with pytest.raises(ValueError):
        stage.model_copy(
            update={"preparation_kind": v2.PreparationKind.COMPILE}
        ).validate_for_manifest(manifest)
    result = fixture("judge-result.valid-python313", v2.JudgementReport).model_dump(mode="json")
    result.update(cases=[], run_cpu_time_us=None, run_memory_peak_bytes=None)
    with pytest.raises(ValueError):
        v2.JudgementReport.model_validate(result)


def test_jvm_heap_is_smaller_than_total_memory_and_threads_share_the_budget():
    manifest = fixture("judge-manifest.valid-java21", v2.JobManifest)
    limits = manifest.run_limits
    assert 32 * v1.MIB <= v2.jvm_heap_bytes(limits) < limits.memory_bytes
    more_threads = limits.model_copy(update={"processes": 32})
    assert v2.jvm_heap_bytes(more_threads) < v2.jvm_heap_bytes(limits)
    with pytest.raises(ValueError):
        v2.jvm_heap_bytes(limits.model_copy(update={"memory_bytes": 128 * v1.MIB}))
    # Ordinary RuntimeError has no infrastructure failure code or trusted memory-limit claim.
    result = fixture("judge-result.valid-java21", v2.JudgementReport).model_dump(mode="json")
    result["cases"][0]["verdict"] = "runtime_error"
    result["verdict"] = "runtime_error"
    v2.JudgementReport.model_validate(result)


def test_v2_wire_rejects_ambiguous_private_values_without_echoing_them():
    data = (FIXTURES / "judge-lease.valid.json").read_bytes()
    duplicate = data.replace(b'"schema_version": 2', b'"schema_version": 2, "schema_version": 2')
    for invalid in (duplicate, b" " * (v1.MAX_WIRE_BYTES + 1), data.decode().encode("utf-16")):
        with pytest.raises(ValueError):
            v1.decode_wire(v2.LeaseGrant, invalid)
    secret = "private-placeholder-never-echo"
    value = json.loads(data)
    value["lease_token"] = secret
    with pytest.raises(ValueError) as error:
        v1.decode_wire(v2.LeaseGrant, json.dumps(value).encode())
    assert secret not in str(error.value)


def test_storage_boundary_reads_both_versions_without_rewriting_or_cross_version_reports():
    old = ROOT / "tests/fixtures/contracts"
    old_manifest = versions.decode_manifest((old / "judge-manifest.valid.json").read_bytes())
    old_report = versions.decode_report((old / "judge-result.valid.json").read_bytes())
    versions.validate_report(old_report, old_manifest, old_report.manifest_digest)
    assert versions.manifest_bytes(old_manifest) == (old / "canonical-manifest.v1.txt").read_bytes()
    manifest = versions.decode_manifest(
        (FIXTURES / "judge-manifest.valid-python313.json").read_bytes()
    )
    report = versions.decode_report((FIXTURES / "judge-result.valid-python313.json").read_bytes())
    versions.validate_report(
        report, manifest, hashlib.sha256(versions.manifest_bytes(manifest)).hexdigest()
    )
    for result, task in ((old_report, manifest), (report, old_manifest)):
        with pytest.raises(ValueError):
            versions.validate_report(result, task, result.manifest_digest)
    payload = (FIXTURES / "judge-manifest.valid-python313.json").read_bytes()
    for invalid in (
        payload.replace(b'"schema_version": 2', b'"schema_version": 3'),
        payload.replace(b'"schema_version": 2', b'"schema_version": true'),
        payload.replace(b'"schema_version": 2', b'"schema_version": 2, "schema_version": 2'),
    ):
        with pytest.raises(ValueError):
            versions.decode_manifest(invalid)


def test_registry_is_explicit_for_each_protocol_and_retains_legacy_configuration():
    from kf_oj.core.settings import Settings

    registry = fixture("judge-registry.valid", v2.AdmissionRegistry)
    original = ROOT / "tests/fixtures/contracts"
    legacy = v1.decode_wire(v1.JobManifest, (original / "judge-manifest.valid.json").read_bytes())
    registry.admit(legacy, 1)
    assert registry.legacy.model_dump(mode="json") == json.loads(
        (original / "judge-admission.valid.json").read_text()
    )
    for language in v2.Language:
        manifest = fixture(f"judge-manifest.valid-{language}", v2.JobManifest)
        registry.admit(manifest, 1)
        with pytest.raises(ValueError, match="protocol"):
            v2.admit_manifest(registry.legacy, manifest, 1)
        for field, value in (("image_digest", "f" * 64), ("runtime_version", "other")):
            changed = manifest.model_copy(
                update={"toolchain": manifest.toolchain.model_copy(update={field: value})}
            )
            with pytest.raises(ValueError):
                registry.admit(changed, 1)
    with pytest.raises(ValueError, match="protocol"):
        v2.admit_manifest(registry.multi_runtime, legacy, 1)
    for policy in (registry.legacy, registry.multi_runtime, registry):
        settings = Settings(_env_file=None, judge_admission=policy.model_dump(mode="json"))
        assert type(settings.judge_admission) is type(policy)
    raw = registry.model_dump(mode="json")
    raw["multi_runtime"]["prefetch"] = 2
    with pytest.raises(ValueError):
        Settings(_env_file=None, judge_admission=raw)
