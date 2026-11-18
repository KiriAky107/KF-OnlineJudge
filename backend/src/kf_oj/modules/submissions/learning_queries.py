"""Owned submission projections. Infrastructure attempts never count as student attempts."""

from sqlalchemy import case, func, select

from kf_oj.modules.submissions.models import Evaluation, Submission
from kf_oj.modules.submissions.public import LearningHistory, LearningProgress, LearningRecord

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
        select(Submission, Evaluation)
        .join(Evaluation, Evaluation.submission_id == Submission.id)
        .where(Submission.account_id == principal.account_id)
    )


async def records(session, principal, *, since, limit):
    if not 1 <= limit <= 500 or since.utcoffset() is None:
        raise ValueError("invalid learning history range")
    rows = list(
        await session.execute(
            owned(principal)
            .where(Submission.created_at >= since)
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(limit + 1)
        )
    )
    return LearningHistory(
        tuple(
            LearningRecord(
                s.id,
                e.id,
                s.problem_id,
                s.problem_version_id,
                s.created_at,
                e.state,
                e.verdict if e.state in {"finished", "failed", "cancelled"} else None,
                e.failure_code,
            )
            for s, e in rows[:limit]
        ),
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


async def progress(session, principal, version_ids, *, since):
    if since.utcoffset() is None:
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
        .subquery()
    )
    q = (
        owned(principal)
        .join(ranked, ranked.c.sid == Submission.id)
        .where(ranked.c.rank <= 10)
        .order_by(Submission.problem_version_id, ranked.c.rank)
    )
    evidence = {vid: [] for vid in version_ids}
    for s, e in await session.execute(q):
        evidence[s.problem_version_id].append(
            LearningRecord(
                s.id,
                e.id,
                s.problem_id,
                s.problem_version_id,
                s.created_at,
                e.state,
                e.verdict,
                e.failure_code,
            )
        )
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
