"""Contract/admission-route tests; Root file/atomicity and drain acceptance are VM-only."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from kf_oj.api.availability import AvailabilityMiddleware, needs_open
from kf_oj.contracts.availability import AvailabilityRecord


@pytest.mark.parametrize("state", ["open", "draining", "quiescent"])
def test_availability_is_exactly_bound_to_a_known_version_and_state(state):
    path = (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/contracts/delivery-availability.valid.json"
    )
    value = json.loads(path.read_text())
    value["state"] = state
    assert AvailabilityRecord.model_validate(value).state == state
    schema = path.parents[3] / "contracts/schemas/delivery-availability.v1.schema.json"
    Draft202012Validator(json.loads(schema.read_text())).validate(value)
    for key, replacement in (
        ("schema_version", True),
        ("release_id", "../release"),
        ("state", "always_allow"),
    ):
        with pytest.raises(ValueError):
            AvailabilityRecord.model_validate({**value, key: replacement})


@pytest.mark.parametrize(
    "method,path,blocked",
    [
        ("POST", "/api/v1/submissions", True),
        ("POST", "/api/v1/admin/problem-imports", True),
        ("POST", "/api/v1/admin/problems/publish", True),
        ("POST", "/api/v1/auth/login", True),
        ("GET", "/api/v1/internal/evaluations/artifacts", False),
        ("POST", "/api/v1/auth/logout", False),
        ("POST", "/api/v1/submissions/11111111-1111-4111-8111-111111111111/cancel", False),
        (
            "PUT",
            "/api/v1/admin/problem-imports/11111111-1111-4111-8111-111111111111/archive",
            False,
        ),
    ],
)
def test_drain_blocks_new_mutations_without_cutting_accepted_reads_and_cancel(
    method, path, blocked
):
    assert needs_open(method, path) is blocked


def test_missing_admission_record_rejects_before_body_or_domain_execution(monkeypatch):
    # Transport policy only: this stub does not verify Root ownership or Linux file safety.
    calls = []
    messages = []

    def missing(*_):
        raise FileNotFoundError("record missing")

    monkeypatch.setattr("kf_oj.api.availability.current", missing)

    async def receive():
        calls.append("body")
        return {"type": "http.request", "body": b""}

    async def domain(*_):
        calls.append("domain")

    async def send(message):
        messages.append(message)

    settings = SimpleNamespace(
        environment="production", delivery_release_id=None, delivery_configuration_id=None
    )
    asyncio.run(
        AvailabilityMiddleware(domain, settings=settings)(
            {"type": "http", "method": "POST", "path": "/api/v1/submissions"}, receive, send
        )
    )
    assert calls == []
    assert messages[0]["status"] == 503
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
