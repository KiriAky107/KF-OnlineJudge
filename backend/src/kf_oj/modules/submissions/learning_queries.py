"""Owned submission projections. Infrastructure attempts never count as student attempts."""

from sqlalchemy import String, and_, case, cast, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on

from kf_oj.contracts.learning_model import (
    ChatSubmissionDetails,
    ReviewEvaluationCategory,
    ReviewEvaluationSummary,
)
from kf_oj.modules.submissions.models import Evaluation, SelfTestPublicComparison, Submission
from kf_oj.modules.submissions.public import (
    LearningHistory,
    LearningPractice,
    LearningProgress,
    LearningRecord,
)

ORDINARY = (
    "accepted",
    "wrong_answer",
    "compilation_error",
    "runtime_error",
    "time_limit_exceeded",
    "memory_limit_exceeded",
    "output_limit_exceeded",
)

_REVIEW_CATEGORIES: dict[str, ReviewEvaluationCategory] = {
    "accepted": "accepted",
    "compilation_error": "compilation_error",
    "wrong_answer": "wrong_answer",
    "runtime_error": "runtime_error",
    "time_limit_exceeded": "resource_limit",
    "memory_limit_exceeded": "resource_limit",
    "output_limit_exceeded": "resource_limit",
}


async def recent_versions(session, principal, *, since, as_of=None):
    query = select(Submission.problem_version_id, Submission.created_at).where(
        Submission.account_id == principal.account_id, Submission.created_at >= since
    )
    if as_of is not None:
        query = query.where(Submission.created_at <= as_of)
    latest = (
        query.ext(distinct_on(Submission.problem_id))
        .order_by(Submission.problem_id, Submission.created_at.desc(), Submission.id.desc())
        .subquery()
    )
    return tuple(
        await session.scalars(
            select(latest.c.problem_version_id)
            .order_by(latest.c.created_at.desc(), latest.c.problem_version_id)
            .limit(8)
        )
    )


def owned(principal):
    return (
        select(
            Submission.id.label("submission_id"),
            Evaluation.id.label("evaluation_id"),
            Submission.problem_id,
            Submission.problem_version_id,
            Submission.created_at,
            Evaluation.updated_at,
            Evaluation.state,
            Evaluation.verdict,
            Evaluation.failure_code,
        )
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(Submission.account_id == principal.account_id)
    )


def _record(row, *, as_of=None) -> LearningRecord:
    visible = as_of is None or row.updated_at <= as_of
    return LearningRecord(
        row.submission_id,
        row.evaluation_id,
        row.problem_id,
        row.problem_version_id,
        row.created_at,
        row.state if visible else "unobserved",
        row.verdict if visible and row.state in {"finished", "failed", "cancelled"} else None,
        row.failure_code if visible else None,
    )


async def records(
    session,
    principal,
    *,
    since,
    limit,
    as_of=None,
    problem_ids=(),
    submission_id=None,
    anchor_submission_id=None,
):
    if not 1 <= limit <= 500 or since.utcoffset() is None:
        raise ValueError("invalid learning history range")
    if as_of is not None and (as_of.utcoffset() is None or as_of < since):
        raise ValueError("invalid learning history cutoff")
    if submission_id is not None and anchor_submission_id is not None:
        raise ValueError("choose one submission scope")
    query = owned(principal).where(Submission.created_at >= since)
    if as_of is not None:
        query = query.where(Submission.created_at <= as_of)
        # Do not let an evaluation updated later displace an observed record in the page.
        if submission_id is None:
            observed = Evaluation.updated_at <= as_of
            if anchor_submission_id is not None:
                observed = or_(observed, Submission.id == anchor_submission_id)
            query = query.where(observed)
    if problem_ids:
        query = query.where(Submission.problem_id.in_(problem_ids))
    if submission_id is not None:
        query = query.where(Submission.id == submission_id)
    if anchor_submission_id is not None:
        selected = select(
            Submission.id, Submission.created_at, Submission.problem_version_id, Submission.language
        ).where(
            Submission.id == anchor_submission_id,
            Submission.account_id == principal.account_id,
            Submission.created_at >= since,
        )
        if as_of is not None:
            selected = selected.where(Submission.created_at <= as_of)
        anchor = selected.subquery()
        query = query.join_from(
            Submission,
            anchor,
            and_(
                Submission.problem_version_id == anchor.c.problem_version_id,
                Submission.language == anchor.c.language,
                tuple_(Submission.created_at, Submission.id)
                <= tuple_(anchor.c.created_at, anchor.c.id),
            ),
        )
    rows = list(
        await session.execute(
            query.order_by(Submission.created_at.desc(), Submission.id.desc()).limit(limit + 1)
        )
    )
    return LearningHistory(
        tuple(_record(row, as_of=as_of) for row in rows[:limit]),
        len(rows) > limit,
    )


async def accepted(session, principal, version_ids, *, as_of=None):
    if not version_ids:
        return set()
    q = (
        select(Submission.problem_version_id)
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(
            Submission.account_id == principal.account_id,
            Submission.problem_version_id.in_(version_ids),
            Evaluation.state == "finished",
            Evaluation.verdict == "accepted",
            Evaluation.failure_code.is_(None),
        )
        .distinct()
    )
    if as_of is not None:
        q = q.where(Submission.created_at <= as_of, Evaluation.updated_at <= as_of)
    return set(await session.scalars(q))


async def review_evaluation_category(
    session, principal, *, submission_id, evaluation_id, problem_version_id, as_of
) -> ReviewEvaluationCategory | None:
    verdict = await session.scalar(
        owned(principal)
        .with_only_columns(Evaluation.verdict, maintain_column_froms=True)
        .where(
            Submission.id == submission_id,
            Submission.problem_version_id == problem_version_id,
            Submission.created_at <= as_of,
            Evaluation.id == evaluation_id,
            Evaluation.updated_at <= as_of,
            Evaluation.state == "finished",
            Evaluation.failure_code.is_(None),
        )
    )
    return _REVIEW_CATEGORIES.get(verdict)


async def guided_submission_details(
    session, principal, *, submission_id, problem_version_id, as_of
) -> ChatSubmissionDetails | None:
    verdict = case(
        (
            and_(
                Evaluation.state == "finished",
                Evaluation.failure_code.is_(None),
                Evaluation.updated_at <= as_of,
            ),
            Evaluation.verdict,
        )
    )
    row = (
        await session.execute(
            owned(principal)
            .with_only_columns(Submission.language, verdict, maintain_column_froms=True)
            .where(
                Submission.id == submission_id,
                Submission.problem_version_id == problem_version_id,
                Submission.created_at <= as_of,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    language, verdict = row
    return ChatSubmissionDetails(
        language=language,
        evaluation=ReviewEvaluationSummary(category=_REVIEW_CATEGORIES.get(verdict)),
    )


async def practice(session, principal, version_ids, *, as_of):
    if as_of.utcoffset() is None:
        raise ValueError("timezone required")
    if not version_ids:
        return {}
    query = (
        select(
            Submission.problem_version_id,
            func.max(Submission.created_at),
            func.max(Submission.created_at).filter(Evaluation.verdict == "accepted"),
            func.min(Submission.created_at).filter(Evaluation.verdict == "accepted"),
        )
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(
            Submission.account_id == principal.account_id,
            Submission.problem_version_id.in_(version_ids),
            Submission.created_at <= as_of,
            Evaluation.updated_at <= as_of,
            Evaluation.state == "finished",
            Evaluation.verdict.in_(ORDINARY),
            Evaluation.failure_code.is_(None),
        )
        .group_by(Submission.problem_version_id)
    )
    return {
        version_id: LearningPractice(attempt, latest_pass, first_pass)
        for version_id, attempt, latest_pass, first_pass in await session.execute(query)
    }


async def progress(session, principal, version_ids, *, since, as_of=None):
    if since.utcoffset() is None or (as_of is not None and as_of.utcoffset() is None):
        raise ValueError("timezone required")
    if not version_ids:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=Submission.problem_version_id,
            order_by=(
                case((Evaluation.verdict == "accepted", 0), else_=1),
                Submission.created_at.desc(),
                Submission.id.desc(),
            ),
        )
        .label("rank")
    )
    ranked = (
        select(Submission.id.label("sid"), rank)
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(
            Submission.account_id == principal.account_id,
            Submission.problem_version_id.in_(version_ids),
            Submission.created_at >= since,
            Evaluation.state == "finished",
            Evaluation.verdict.in_(ORDINARY),
            Evaluation.failure_code.is_(None),
        )
    )
    if as_of is not None:
        ranked = ranked.where(Submission.created_at <= as_of, Evaluation.updated_at <= as_of)
    ranked = ranked.subquery()
    q = (
        owned(principal)
        .join(ranked, ranked.c.sid == Submission.id)
        .where(ranked.c.rank <= 10)
        .order_by(Submission.problem_version_id, ranked.c.rank)
    )
    evidence = {vid: [] for vid in version_ids}
    for row in await session.execute(q):
        evidence[row.problem_version_id].append(_record(row))
    return {
        vid: LearningProgress(
            "completed"
            if any(r.verdict == "accepted" for r in rows)
            else "attempted"
            if rows
            else "not_started",
            tuple(rows),
        )
        for vid, rows in evidence.items()
    }


async def team_verdict_totals(session, cohort, version_ids, *, as_of):
    if not version_ids:
        return {}
    representative = (
        select(Submission.problem_version_id.label("version_id"), Evaluation.verdict)
        .select_from(Submission)
        .join(cohort, cohort.c.account_id == Submission.account_id)
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(
            Submission.problem_version_id.in_(version_ids),
            Submission.created_at.between(cohort.c.since, as_of),
            Evaluation.updated_at <= as_of,
            Evaluation.state == "finished",
            Evaluation.verdict.in_(ORDINARY),
            Evaluation.failure_code.is_(None),
        )
        .ext(distinct_on(Submission.problem_version_id, Submission.account_id))
        .order_by(
            Submission.problem_version_id,
            Submission.account_id,
            case((Evaluation.verdict == "accepted", 0), else_=1),
            Submission.created_at.desc(),
            Submission.id.desc(),
        )
        .subquery()
    )
    totals = select(representative.c.version_id, representative.c.verdict, func.count()).group_by(
        representative.c.version_id, representative.c.verdict
    )
    grouped = {}
    for version, verdict, count in await session.execute(totals):
        grouped.setdefault(version, {})[verdict] = count
    return grouped


async def acceptance_totals(session, cohort) -> dict[str, tuple[int, int, int]]:
    grade = (
        select(1)
        .select_from(Submission)
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(
            Submission.account_id == cohort.c.account_id,
            Submission.problem_version_id == cohort.c.version_id,
            Evaluation.state == "finished",
            Evaluation.failure_code.is_(None),
        )
        .correlate(cohort)
    )
    already_passed = grade.where(
        Submission.created_at < cohort.c.since,
        Evaluation.updated_at < cohort.c.since,
        Evaluation.verdict == "accepted",
    ).exists()
    during = grade.where(
        Submission.created_at >= cohort.c.since,
        Submission.created_at < cohort.c.until,
        Evaluation.updated_at < cohort.c.until,
        Evaluation.verdict.in_(ORDINARY),
    )
    rows = await session.execute(
        select(
            cohort.c.source,
            func.count(),
            func.count().filter(during.exists()),
            func.count().filter(during.where(Evaluation.verdict == "accepted").exists()),
        )
        .select_from(cohort)
        .where(~already_passed)
        .group_by(cohort.c.source)
    )
    return {
        source: (assigned, attempted, accepted) for source, assigned, attempted, accepted in rows
    }


async def self_test_comparison_totals(session, start, end) -> dict[bool, tuple[int, int, int]]:
    report = Evaluation.final_report
    binding = report["binding"]
    output = report["self_test_output"]
    stdout = output["stdout"].astext
    cases = report["cases"]
    case_count = case(
        (func.jsonb_typeof(cases) == "array", func.jsonb_array_length(cases)), else_=0
    )
    comparable = and_(
        Evaluation.verdict == "accepted",
        Evaluation.failure_code.is_(None),
        binding["evaluation_id"].astext == cast(Evaluation.id, String),
        binding["attempt_id"].astext == cast(Evaluation.current_attempt_id, String),
        binding["fencing_version"].astext == cast(Evaluation.fencing_version, String),
        case_count == 1,
        cases[0]["case_id"].astext == cast(SelfTestPublicComparison.self_test_id, String),
        stdout.is_not(None),
        output["stdout_truncated"].astext == "false",
        func.strpos(stdout, "\ufffd") == 0,
    )
    mismatch = func.sha256(func.convert_to(stdout, "UTF8")) != func.decode(
        SelfTestPublicComparison.expected_digest, "hex"
    )
    linked = SelfTestPublicComparison.diagnostic_request_id.is_not(None)
    rows = await session.execute(
        select(
            linked,
            func.count(),
            func.count().filter(comparable),
            func.count().filter(comparable, mismatch),
        )
        .select_from(SelfTestPublicComparison)
        .join(Evaluation, Evaluation.self_test_id == SelfTestPublicComparison.self_test_id)
        .where(
            Evaluation.state == "finished",
            Evaluation.updated_at >= start,
            Evaluation.updated_at < end,
        )
        .group_by(linked)
    )
    return {
        origin: (completed, compared, different) for origin, completed, compared, different in rows
    }
