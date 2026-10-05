import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from kf_oj.contracts.judge import JudgeTaskNotification

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts"
SCHEMA = json.loads((ROOT / "contracts/schemas/judge-task.v1.schema.json").read_text())


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("judge-task.*.json")), ids=lambda p: p.stem)
def test_shared_wire_examples(path: Path) -> None:
    payload = json.loads(path.read_text())
    validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    if ".invalid-" in path.name:
        with pytest.raises(ValidationError):
            JudgeTaskNotification.model_validate(payload)
        assert not validator.is_valid(payload)
    else:
        notification = JudgeTaskNotification.model_validate(payload)
        validator.validate(payload)
        assert notification.model_dump(mode="json") == payload
