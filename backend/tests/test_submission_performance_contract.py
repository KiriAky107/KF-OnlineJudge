"""Protocol/route boundaries, not evidence of real measured execution."""

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from kf_oj.api.app import create_app
from kf_oj.api.dependencies import current_principal
from kf_oj.contracts.submission_performance import SubmissionPerformance
from kf_oj.core.settings import Settings
from kf_oj.modules.identity.public import Principal

ROOT = Path(__file__).parents[2]


def fixture():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/submission-performance.valid.json").read_text()
    )


def test_decimal_measurements_zero_and_four_languages_roundtrip():
    data = fixture()
    for language in ("c17", "cpp20", "python313", "java21"):
        data["language"] = language
        response = SubmissionPerformance.model_validate(data)
        encoded = response.model_dump(mode="json")
        assert encoded == data
        assert encoded["cpu"]["current"] == "9007199254740993"
        assert encoded["memory"]["current"] == "0"
        Draft202012Validator(SubmissionPerformance.model_json_schema()).validate(encoded)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(language="Rust"),
        lambda v: v.update(schema_version=True),
        lambda v: v.update(username="other_user"),
        lambda v: v.update(comparison_group=None),
        lambda v: v.update(as_of="2026-10-11T00:00:00"),
        lambda v: v["cpu"].update(current=9007199254740993),
        lambda v: v["cpu"].update(current="092"),
        lambda v: v["cpu"].update(current="9223372036854775808"),
        lambda v: v["cpu"].update(sample_count=4),
        lambda v: v["cpu"].update(strictly_slower_count=6, tied_count=1),
        lambda v: v["cpu"].update(oldest_sample_at="2026-10-12T00:00:00Z"),
        lambda v: v["cpu"]["buckets"][0].update(upper_inclusive=True),
        lambda v: v["cpu"]["buckets"][0].update(count=2),
        lambda v: v["cpu"]["buckets"][1].update(start="9007199254740994"),
        lambda v: v["memory"].update(sample_count=5),
        lambda v: v["memory"].update(tied_count=1),
        lambda v: v["memory"].update(reason="measurement_missing"),
    ],
)
def test_no_rounded_fabricated_unbounded_or_inconsistent_aggregate(change):
    data = copy.deepcopy(fixture())
    change(data)
    with pytest.raises(ValueError):
        SubmissionPerformance.model_validate(data)


def test_performance_unconfigured_and_bad_history_filters_fail_closed():
    app = create_app(Settings(_env_file=None, environment="test"))
    sid = "11111111-1111-4111-8111-111111111111"
    with TestClient(app) as client:
        assert client.get(f"/api/v1/submissions/{sid}/performance").status_code == 503
        app.dependency_overrides[current_principal] = lambda: Principal(UUID(sid), "owner")
        response = client.get(f"/api/v1/submissions/{sid}/performance")
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/api/v1/submissions?language=rust").status_code == 422
        assert client.get("/api/v1/submissions?problem_version_id=1001").status_code == 422
