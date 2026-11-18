import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from kf_oj.contracts.problem_learning import LearningMetadataUpdate
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.problems.learning_metadata import project
from kf_oj.modules.submissions.public import accepted_versions, learning_progress, learning_records


def test_metadata_is_optional_and_old_version_remains_accessible():
    version = SimpleNamespace(id=uuid4(), title="题目", published_at=datetime.now(UTC))
    problem = SimpleNamespace(id=uuid4(), slug="test", enabled=True, active_version_id=uuid4())
    item = project(problem, version, None)
    assert item.available and not item.is_current_version
    assert item.tags == () and item.difficulty is None and item.metadata_revision == 0
    problem.enabled = False
    assert not project(problem, version, None).available


def test_metadata_labels_are_real_unique_labels():
    assert LearningMetadataUpdate(tags=["动态规划"], difficulty=None).difficulty is None
    for tags in [[""], [" dp"], ["dp", "dp"]]:
        with pytest.raises(ValidationError):
            LearningMetadataUpdate(tags=tags, difficulty="easy")


class Session:
    def __init__(self):
        self.queries = []

    async def execute(self, q):
        self.queries.append(q)
        return []

    async def scalars(self, q):
        self.queries.append(q)
        return []


def test_owned_queries_keep_history_and_progress_ranges_independent():
    asyncio.run(check_owned_queries())


async def check_owned_queries():
    session = Session()
    principal = Principal(uuid4(), "learner")
    version = uuid4()
    since = datetime(2020, 1, 1, tzinfo=UTC)
    assert not (await learning_records(session, principal, since=since)).truncated
    assert await accepted_versions(session, principal, {version}) == set()
    result = await learning_progress(session, principal, {version}, since=since)
    assert result[version].status == "not_started"
    sql = [
        str(q.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for q in session.queries
    ]
    assert all(str(principal.account_id) in q for q in sql)
    assert "LIMIT 501" in sql[0]
    assert "created_at >=" not in sql[1] and str(version) in sql[1]
    assert "created_at >=" in sql[2] and "row_number()" in sql[2]
    assert "judge_attempts" not in "".join(sql)


def test_candidate_search_is_bounded_and_only_selection_locks():
    asyncio.run(check_candidate_queries())


async def check_candidate_queries():
    from kf_oj.modules.problems.public import learning_candidates, learning_selected

    session = Session()
    assert await learning_candidates(session, tags=("dp", "graph"), difficulty="easy") == ()
    selected_id = uuid4()
    assert await learning_selected(session, {selected_id}) == {}
    sql = [str(q.compile(dialect=postgresql.dialect())) for q in session.queries]
    assert "LIMIT" in sql[0] and "FOR SHARE" not in sql[0]
    assert list(session.queries[0].compile().params.values()).count(["dp", "graph"]) == 1
    assert "@>" in sql[0] and "difficulty =" in sql[0]
    assert "FOR SHARE OF problems" in sql[1] and "problem_versions.id IN" in sql[1]
    with pytest.raises(ValueError):
        await learning_selected(session, {uuid4() for _ in range(11)})
