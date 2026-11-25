"""Use owned practice and plan dates to space repeated recommendations."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text

from kf_oj.contracts.learning import FirstAcceptanceKind, PracticeStatus, TrainingTiming
from kf_oj.modules.learning.models import HintUsage
from kf_oj.modules.submissions.public import LearningPractice

RECENT_PRACTICE = timedelta(days=7)
REVIEW_INTERVAL = timedelta(days=14)
ASSISTED_REVIEW_INTERVAL = timedelta(days=7)


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


async def first_hint_dates(
    session, owner_id: UUID, version_ids: set[UUID], now: datetime
) -> dict[UUID, datetime]:
    if not version_ids:
        return {}
    rows = await session.execute(
        select(HintUsage.version_id, HintUsage.first_provided_at).where(
            HintUsage.owner_id == owner_id,
            HintUsage.version_id.in_(version_ids),
            HintUsage.first_provided_at <= now,
        )
    )
    return {version_id: provided_at for version_id, provided_at in rows}


def training_timing(
    practice: LearningPractice | None,
    last_planned_at: datetime | None,
    now: datetime,
    *,
    first_hint_at: datetime | None = None,
) -> TrainingTiming | None:
    if practice is None and last_planned_at is None:
        return None

    last_attempt_at = practice.last_attempt_at if practice else None
    last_accepted_at = practice.last_accepted_at if practice else None
    first_pass = practice.first_accepted_at if practice else None
    first_acceptance_kind: FirstAcceptanceKind | None = None
    if first_pass is not None:
        first_acceptance_kind = (
            "assisted"
            if first_hint_at is not None and first_hint_at < first_pass
            else "independent"
        )
    review_interval = (
        ASSISTED_REVIEW_INTERVAL if first_acceptance_kind == "assisted" else REVIEW_INTERVAL
    )
    due_at = last_accepted_at + review_interval if last_accepted_at else None
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
        first_acceptance_kind=first_acceptance_kind,
    )
