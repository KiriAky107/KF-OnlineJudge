"""Focused tests for the owner-scoped, same-problem comparison projection."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kf_oj.api.app import create_app
from kf_oj.api.dependencies import current_principal
from kf_oj.core.settings import Settings
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.queries import SubmissionQueries


@pytest.mark.parametrize(
    "kind", ["same_version", "historical_version", "other_problem", "other_left", "other_right"]
)
def test_compare_requires_two_owned_submissions_before_source_reads(kind):
    owner, stranger, problem, version = (uuid4() for _ in range(4))
    now = datetime.now(UTC)
    left = SimpleNamespace(
        id=uuid4(),
        account_id=stranger if kind == "other_left" else owner,
        problem_id=problem,
        problem_version_id=version,
        language="c17",
        created_at=now,
    )
    right = SimpleNamespace(
        id=uuid4(),
        account_id=stranger if kind == "other_right" else owner,
        problem_id=uuid4() if kind == "other_problem" else problem,
        problem_version_id=uuid4() if kind == "historical_version" else version,
        language="c17",
        created_at=now,
    )
    fixture = Path(__file__).parents[2] / "tests/fixtures/contracts/judge-result.valid.json"

    def finished(verdict):
        evaluation_id = uuid4()
        report = json.loads(fixture.read_text())
        report["binding"]["evaluation_id"] = str(evaluation_id)
        report["verdict"] = report["cases"][-1]["verdict"] = verdict
        return SimpleNamespace(
            id=evaluation_id,
            state="finished",
            updated_at=now,
            attempts_count=1,
            failure_code=None,
            verdict=verdict,
            final_report=report,
        )

    rows = {left.id: (left, finished("wrong_answer")), right.id: (right, finished("accepted"))}

    async def execute(query):
        submission_id, account_id = query.compile().params.values()
        assert account_id == owner
        row = rows[submission_id]
        return SimpleNamespace(one_or_none=lambda: row if row[0].account_id == owner else None)

    session = SimpleNamespace(execute=execute)
    sessions = MagicMock()
    sessions.return_value.__aenter__.return_value = session
    queries = SubmissionQueries(sessions, MagicMock())
    queries._read_source = AsyncMock(side_effect=[b"left body", b"right body"])
    app = create_app(Settings(_env_file=None, environment="test"))
    app.dependency_overrides[current_principal] = lambda: Principal(owner, "reviewer")
    with TestClient(app) as client:
        app.state.submission_queries = queries
        response = client.get(f"/api/v1/submissions/{left.id}/compare/{right.id}")
    assert response.headers["Cache-Control"] == "no-store"
    if kind.startswith("other"):
        assert response.status_code == 404
        assert response.json() == {"detail": "submission comparison unavailable"}
        queries._read_source.assert_not_awaited()
        return

    assert response.status_code == 200
    result = response.json()
    assert (result["left"]["submission_id"], result["right"]["submission_id"]) == (
        str(left.id),
        str(right.id),
    )
    assert (result["left_source"], result["right_source"]) == ("left body", "right body")
    assert result["same_version"] is (kind == "same_version")
    assert result["left"]["verdict"] == "wrong_answer"
    assert result["right"]["verdict"] == "accepted"
    assert result["left"]["result"]["cases"][-1]["verdict"] == "wrong_answer"
    assert result["right"]["result"]["cases"][-1]["verdict"] == "accepted"
    assert all(key not in response.text for key in ("case_id", "digest", "binding", "program"))
