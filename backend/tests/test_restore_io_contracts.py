"""Shared ordinary wire examples; no privileged copy or execution acceptance."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kf_oj.contracts.restore_io import RestoreIOReply, RestoreIORequest

CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "tests/fixtures/restore-io/examples.json").read_bytes()
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_shared_restore_io_examples(case):
    model = RestoreIORequest if case["direction"] == "request" else RestoreIOReply
    if case["valid"]:
        assert model.model_validate(case["value"]).model_dump(mode="json") == case["value"]
    else:
        with pytest.raises(ValidationError):
            model.model_validate(case["value"])
