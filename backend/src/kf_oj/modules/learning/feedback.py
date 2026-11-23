"""Use recent learner self-assessments as secondary recommendation evidence."""

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select

from kf_oj.contracts.learning import Difficulty, TrainingFeedbackEvidence
from kf_oj.modules.learning.models import TrainingFeedbackRow

_LEVELS: tuple[Difficulty, ...] = ("easy", "medium", "hard")
_ADJUSTMENT = {"too_easy": 1, "appropriate": 0, "too_hard": -1}
_REASON = {"too_easy": "偏简单", "appropriate": "难度合适", "too_hard": "偏难"}
_DIFFICULTY_LABEL = {"easy": "简单", "medium": "中等", "hard": "困难"}


async def recent_feedback(session, owner_id, now) -> tuple[TrainingFeedbackRow, ...]:
    found = await session.scalars(
        select(TrainingFeedbackRow)
        .where(
            TrainingFeedbackRow.owner_id == owner_id,
            TrainingFeedbackRow.rated_at.between(now - timedelta(days=90), now),
            TrainingFeedbackRow.difficulty.is_not(None),
        )
        .order_by(TrainingFeedbackRow.rated_at.desc(), TrainingFeedbackRow.item_id)
        .limit(50)
    )
    return tuple(found)


def matching_feedback(
    tags: Iterable[str], latest: tuple[TrainingFeedbackRow, ...]
) -> TrainingFeedbackEvidence | None:
    tags = set(tags)
    for row in latest:
        if not tags.intersection(row.tags):
            continue
        position = _LEVELS.index(row.difficulty)
        next_level = _LEVELS[max(0, min(2, position + _ADJUSTMENT[row.rating]))]
        return TrainingFeedbackEvidence(
            training_id=row.plan_id,
            training_item_id=row.item_id,
            problem_version_id=row.version_id,
            rating=row.rating,
            rated_at=row.rated_at,
            preferred_difficulty=next_level,
        )
    return None


def reason(feedback: TrainingFeedbackEvidence) -> str:
    return (
        f"本人同知识点自评「{_REASON[feedback.rating]}」，"
        f"建议下一轮优先练习{_DIFFICULTY_LABEL[feedback.preferred_difficulty]}难度"
    )
