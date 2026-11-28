import asyncio
from base64 import b64decode
from dataclasses import replace

import pytest
from test_diagnostic_batch_replay import batch

from kf_oj.contracts.diagnostic_batch import DiagnosticBatchManifest
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.modules.learning.diagnostic_batch import (
    DiagnosticInputCheck,
    draft_counted_list_reduction,
)
from kf_oj.modules.learning.diagnostic_inputs import DiagnosticInputsUnavailable
from kf_oj.workers import diagnostic_batch as command


def counted_list(source, *, recipe="counted-i64-list-v1"):
    fixture = batch()

    def member(name, data):
        return {"name": name, "artifact": ArtifactStore.reference(data)}

    raw = fixture.manifest.model_dump()
    raw["generation"]["recipe"] = recipe
    raw["cases"][0]["input"] = member("original", source)
    raw["input_checker"] = {
        "constraints_revision": member("constraints", b"independent signed-integer bounds"),
        "source": member("checker", b"reviewed checker source"),
    }
    return replace(
        fixture,
        manifest=DiagnosticBatchManifest.model_validate(raw),
        cases=((source, fixture.cases[0][1]), fixture.cases[1]),
        input_check=DiagnosticInputCheck(fixture.configuration, b"reviewed checker source"),
    )


def test_registered_reduction_command_returns_a_stable_private_proposal(monkeypatch, tmp_path):
    original = b"6\n-9223372036854775808 4 -7 3 8 9223372036854775807\n"
    material = counted_list(original)

    async def load(path, digest, settings, admission):
        assert path == tmp_path and digest == material.manifest_sha256
        assert settings.environment == "test"
        return material

    monkeypatch.setattr(command, "load_registered_batch", load)
    monkeypatch.setattr(command, "Settings", lambda: Settings(_env_file=None, environment="test"))
    response = asyncio.run(
        command.run(tmp_path, material.manifest_sha256, registry=True, propose_reduction=True)
    )
    assert response == draft_counted_list_reduction(material)
    assert response["state"] == "verification_pending"
    assert response["original_input_sha256"] == ArtifactStore.reference(original).digest
    cases = response["candidates"]
    assert len(cases) == 3
    assert [item["artifact"]["bytes"] for item in cases] == sorted(
        item["artifact"]["bytes"] for item in cases
    )
    for candidate in cases:
        data = b64decode(candidate["input_base64"], validate=True)
        assert data.startswith(b"3\n") and len(data) < len(original)
        assert candidate["artifact"] == ArtifactStore.reference(data).model_dump(mode="json")
        assert "expected" not in candidate
    with pytest.raises(ValueError, match="registered"):
        asyncio.run(command.run(tmp_path, material.manifest_sha256, propose_reduction=True))


@pytest.mark.parametrize("data", [b"3\n1 2\n", b"2\n1 9223372036854775808\n", b"2\na 2\n"])
def test_reduction_proposals_reject_out_of_format_inputs(data):
    with pytest.raises(DiagnosticInputsUnavailable, match="counted-list"):
        draft_counted_list_reduction(counted_list(data))


def test_no_reduction_without_an_independent_matching_source():
    one = counted_list(b"1\n7\n")
    result = draft_counted_list_reduction(one)
    assert result["state"] == "no_smaller_proposal" and result["candidates"] == []
    with pytest.raises(DiagnosticInputsUnavailable, match="approved list recipe"):
        draft_counted_list_reduction(counted_list(b"2\n7 9\n", recipe="other-format"))
    changed = replace(one, cases=((b"1\n8\n", None), one.cases[1]))
    with pytest.raises(DiagnosticInputsUnavailable, match="approved input"):
        draft_counted_list_reduction(changed)
