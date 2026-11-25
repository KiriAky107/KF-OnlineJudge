"""Explicit synthetic records for deterministic learning rules, not live judgement."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from kf_oj.contracts.agent_learning import TrainingCandidatesArguments
from kf_oj.contracts.learning import LearningParameters
from kf_oj.modules.learning.practice import training_timing
from kf_oj.modules.learning.service import rank_candidates, summarize
from kf_oj.modules.problems.public import LearningCandidate
from kf_oj.modules.submissions.public import LearningHistory, LearningPractice, LearningRecord

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def candidate(n, **changes):
    c = LearningCandidate(
        problem_id=UUID(int=n),
        version_id=UUID(int=n + 100),
        title=f"题目{n}",
        tags=("排序",),
        difficulty="easy",
        metadata_revision=1,
        available=True,
        slug=f"p{n}",
        is_current_version=True,
    )
    return replace(c, **changes)


def record(n, c, verdict="wrong_answer", **changes):
    r = LearningRecord(
        UUID(int=n + 1000),
        UUID(int=n + 2000),
        c.problem_id,
        c.version_id,
        NOW - timedelta(days=1),
        "finished",
        verdict,
        None,
    )
    return replace(r, **changes)


def test_assisted_first_pass_brings_review_forward():
    independent, assisted = candidate(1), candidate(2)
    accepted = NOW - timedelta(days=8)
    practice = LearningPractice(accepted, accepted, accepted)
    assisted_timing = training_timing(
        practice, None, NOW, first_hint_at=accepted - timedelta(days=1)
    )
    independent_timing = training_timing(
        practice, None, NOW, first_hint_at=accepted + timedelta(days=1)
    )
    assert assisted_timing.first_acceptance_kind == "assisted"
    assert assisted_timing.review_due_at == accepted + timedelta(days=7)
    assert independent_timing.first_acceptance_kind == "independent"
    assert independent_timing.review_due_at == accepted + timedelta(days=14)
    ranked = rank_candidates(
        (independent, assisted),
        LearningParameters(count=2),
        LearningHistory((), False),
        {},
        {independent.version_id, assisted.version_id},
        NOW,
        timings={
            independent.version_id: independent_timing,
            assisted.version_id: assisted_timing,
        },
    )
    assert [item.problem_id for item in ranked.items] == [
        assisted.problem_id,
        independent.problem_id,
    ]
    assert "7 天" in ranked.items[0].reasons[-1]
    assert ranked.rule_version == "learning-rules-v7"


def test_statistics_count_submissions_not_retries_and_exclude_infrastructure():
    c = candidate(1)
    a = record(1, c, "accepted")
    rows = (
        a,
        a,
        record(2, c),
        record(3, c, "system_error", state="failed", failure_code="lease_lost"),
        record(4, c, None, state="queued"),
    )
    result = summarize(LearningHistory(rows, True), {c.version_id: c}, NOW)
    assert result.submission_count == 4
    assert result.ordinary_sample_count == 2
    assert result.verdict_counts == {"accepted": 1, "wrong_answer": 1}
    assert result.tags[0].non_accepted_count == 1
    assert result.truncated and result.since == NOW - timedelta(days=90)


def test_logical_problem_dedup_does_not_transfer_version_acceptance():
    old = candidate(1)
    new = replace(old, version_id=UUID(int=999))
    result = summarize(
        LearningHistory((record(1, old, "accepted"), record(2, new)), False),
        {old.version_id: old, new.version_id: new},
        NOW,
    )
    assert result.distinct_problem_count == result.accepted_problem_count == 1
    assert result.accepted_version_ids == (old.version_id,)
    assert result.metadata_revisions[new.version_id] == 1


def test_cold_start_is_empty_not_fabricated():
    result = summarize(LearningHistory((), False), {}, NOW)
    assert result.submission_count == 0
    assert result.tags == result.recent == ()
    assert result.earliest_record_at is None


def test_ranking_uses_full_history_acceptance_outside_overview_window():
    a, b = candidate(1), candidate(2)
    result = rank_candidates(
        (a, b), LearningParameters(), LearningHistory((), True), {}, {a.version_id}, NOW
    )
    assert [i.problem_id for i in result.items] == [b.problem_id, a.problem_id]
    assert result.items[1].previously_accepted


def test_accepted_version_old_failure_no_longer_unresolved_evidence():
    a, b = candidate(1), candidate(2)
    result = rank_candidates(
        (b,),
        LearningParameters(),
        LearningHistory((record(1, a),), False),
        {a.version_id: a},
        {a.version_id},
        NOW,
    )
    assert result.items[0].evidence == ()
    assert not any("未解决" in r for r in result.items[0].reasons)


def test_matching_filters_missing_metadata_and_unavailable_then_stable_dedup():
    a, b = candidate(1), candidate(2)
    unknown = candidate(3, tags=(), difficulty=None, metadata_revision=0)
    disabled = candidate(4, available=False)
    duplicate = replace(b, version_id=UUID(int=999))
    args = (
        LearningParameters(tags=("排序",), difficulty="easy"),
        LearningHistory((), False),
        {},
        set(),
        NOW,
    )
    x = rank_candidates((b, unknown, a, disabled, duplicate), *args)
    y = rank_candidates((duplicate, disabled, a, unknown, b), *args)
    assert x.items == y.items
    assert len(x.items) == 2
    assert x.gaps


def test_goal_coverage_can_choose_two_narrower_problems_over_one_broad_problem():
    broad = candidate(1, tags=("A", "B", "C", "D"))
    left = candidate(2, tags=("A", "B", "E"))
    right = candidate(3, tags=("C", "D", "F"))
    goals = ("A", "B", "C", "D", "E", "F")
    strict = LearningParameters(tags=goals, count=2)
    legacy = '{"tags":["A","B","C","D","E","F"],"difficulty":null,"count":2}'
    assert strict.model_dump_json() == legacy
    assert not TrainingCandidatesArguments.model_validate_json(
        legacy, strict=True
    ).allowed_difficulties
    pool = (right, broad, left)
    history = LearningHistory((), False)
    assert not rank_candidates(pool, strict, history, {}, set(), NOW).items

    cover = strict.model_copy(update={"cover_each_goal": True})
    result = rank_candidates(pool, cover, history, {}, set(), NOW)
    assert [item.problem_id for item in result.items] == [left.problem_id, right.problem_id]
    assert result.goal_coverage.covered == goals and not result.goal_coverage.missing
    fewer = rank_candidates(pool, cover.model_copy(update={"count": 1}), history, {}, set(), NOW)
    assert fewer.items[0].problem_id == broad.problem_id
    assert fewer.goal_coverage.missing == ("E", "F")


def test_plan_projection_preserves_snapshot_and_version_progress():
    from types import SimpleNamespace

    from kf_oj.modules.learning.service import LearningService
    from kf_oj.modules.submissions.public import LearningProgress

    c = candidate(1)
    recommendation = rank_candidates(
        (c,), LearningParameters(), LearningHistory((), False), {}, set(), NOW
    ).items[0]
    plan = SimpleNamespace(
        id=UUID(int=50),
        title="计划",
        created_at=NOW - timedelta(days=120),
        rule_version="old-rule",
        parameters={},
        max_hint_level="L2",
    )
    row = SimpleNamespace(
        id=UUID(int=51),
        version_id=c.version_id,
        position=0,
        note="复习",
        snapshot=recommendation.model_dump(mode="json"),
    )
    ac = record(1, c, "accepted", created_at=NOW - timedelta(days=100))
    current = replace(
        c, title="新标题", metadata_revision=2, available=False, is_current_version=False
    )
    view = LearningService._project(
        plan, [row], {c.version_id: current}, {c.version_id: LearningProgress("completed", (ac,))}
    )
    assert view.completed_count == 1
    assert view.max_hint_level == "L2"
    assert view.items[0].title == c.title
    assert view.items[0].metadata_revision == 1
    assert not view.items[0].available and not view.items[0].is_current_version
    assert view.items[0].progress_evidence[0].submitted_at < NOW - timedelta(days=90)
    other = LearningService._project(
        plan, [row], {c.version_id: current}, {UUID(int=999): LearningProgress("completed", (ac,))}
    )
    assert other.completed_count == 0


def test_idempotent_create_returns_existing_without_recommending():
    import asyncio
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kf_oj.contracts.learning import CreateTraining
    from kf_oj.modules.learning.public import IdempotencyConflict
    from kf_oj.modules.learning.service import LearningService

    body = CreateTraining(idempotency_key=UUID(int=55))
    plan = SimpleNamespace(
        id=UUID(int=56), request_digest=hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    )

    class Session:
        execute = AsyncMock()
        scalar = AsyncMock(return_value=plan)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    service = LearningService(SimpleNamespace(begin=Session))
    service._detail = AsyncMock(return_value="saved")
    service._recommend = AsyncMock(side_effect=AssertionError("must not regenerate"))
    principal = SimpleNamespace(account_id=UUID(int=57))
    assert asyncio.run(service.create(principal, body)) == "saved"
    import pytest

    with pytest.raises(IdempotencyConflict):
        asyncio.run(service.create(principal, body.model_copy(update={"title": "different"})))


def test_foreign_plan_note_and_detail_refused_by_owner_query():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from kf_oj.contracts.learning import UpdateTrainingNote
    from kf_oj.modules.learning.public import LearningUnavailable
    from kf_oj.modules.learning.service import LearningService

    class Session:
        scalar = AsyncMock(return_value=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    service = LearningService(SimpleNamespace(begin=Session))
    principal = SimpleNamespace(account_id=UUID(int=71))
    with pytest.raises(LearningUnavailable):
        asyncio.run(
            service.note(principal, UUID(int=72), UUID(int=73), UpdateTrainingNote(note="no"))
        )
    query = Session.scalar.call_args.args[0]
    values = query.compile().params.values()
    assert principal.account_id in values and UUID(int=72) in values and UUID(int=73) in values
    with pytest.raises(LearningUnavailable):
        asyncio.run(service._detail(Session(), principal, UUID(int=72)))
    assert principal.account_id in Session.scalar.call_args.args[0].compile().params.values()


def test_target_tags_require_all_requested_labels():
    a = candidate(1)
    b = candidate(2, tags=("排序", "数组"))
    result = rank_candidates(
        (a, b),
        LearningParameters(tags=("排序", "数组")),
        LearningHistory((), False),
        {},
        set(),
        NOW,
    )
    assert [item.problem_id for item in result.items] == [b.problem_id]
