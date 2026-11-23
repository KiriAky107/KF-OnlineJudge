"""Owned submission projections. Infrastructure attempts never count as student attempts."""

from sqlalchemy import and_, case, func, or_, select, tuple_

from kf_oj.modules.submissions.models import Evaluation, Submission
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


async def accepted(session, principal, version_ids):
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
    return set(await session.scalars(q))


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
        version_id: LearningPractice(attempt, success)
        for version_id, attempt, success in await session.execute(query)
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
