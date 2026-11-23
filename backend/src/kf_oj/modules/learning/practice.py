"""Use owned practice and plan dates to space repeated recommendations."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from kf_oj.contracts.learning import PracticeStatus, TrainingTiming
from kf_oj.modules.submissions.public import LearningPractice

RECENT_PRACTICE = timedelta(days=7)
REVIEW_INTERVAL = timedelta(days=14)


async def recent_plan_dates(
    session, owner_id: UUID, problem_ids: set[UUID], now: datetime
) -> dict[UUID, datetime]:
    if not problem_ids:
        return {}
    rows = await session.execute(
        text("SELECT * FROM public.kf_recent_training_problems(:owner, :ids, :since, :as_of)"),
        {
            "owner": owner_id,
            "ids": sorted(problem_ids),
            "since": now - RECENT_PRACTICE,
            "as_of": now,
        },
    )
    return {problem: date for problem, date in rows}


def training_timing(
    practice: LearningPractice | None, last_planned_at: datetime | None, now: datetime
) -> TrainingTiming | None:
    if practice is None and last_planned_at is None:
        return None

    last_attempt_at = practice.last_attempt_at if practice else None
    last_accepted_at = practice.last_accepted_at if practice else None
    due_at = last_accepted_at + REVIEW_INTERVAL if last_accepted_at else None
    recent = now - RECENT_PRACTICE
    status: PracticeStatus
    if last_attempt_at is not None and last_attempt_at >= recent:
        status = "recent_attempt"
    elif last_planned_at is not None and last_planned_at >= recent:
        status = "recent_plan"
    elif due_at is None:
        status = "practice"
    elif due_at <= now:
        status = "review_due"
    else:
        status = "review_later"
    return TrainingTiming(
        status=status,
        last_attempt_at=last_attempt_at,
        last_accepted_at=last_accepted_at,
        last_planned_at=last_planned_at,
        review_due_at=due_at,
    )
