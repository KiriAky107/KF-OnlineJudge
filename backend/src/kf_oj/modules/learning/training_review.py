"""Team teaching topics from current members' aggregate training outcomes."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from kf_oj.contracts.learning import TeachingTopic, TeamTrainingReview, TeamTrainingReviewItem
from kf_oj.modules.problems import public as problems
from kf_oj.modules.submissions.public import team_training_verdict_totals
from kf_oj.modules.teams.public import active_team_learners

from .models import LearningPlan, TrainingPublicationItemRow

_MIN_GROUP = 5
_TOPICS: tuple[tuple[TeachingTopic, tuple[str, ...]], ...] = (
    ("correctness", ("wrong_answer",)),
    ("resource_use", ("time_limit_exceeded", "memory_limit_exceeded", "output_limit_exceeded")),
    ("runtime", ("runtime_error",)),
    ("compilation", ("compilation_error",)),
)


def _shared_topic(verdicts: dict[str, int]) -> TeachingTopic:
    categories = {
        topic: sum(verdicts.get(verdict, 0) for verdict in outcomes) for topic, outcomes in _TOPICS
    }
    strongest = max(categories.values())
    candidates = [topic for topic, count in categories.items() if count == strongest]
    if len(candidates) == 1 and strongest >= _MIN_GROUP:
        return candidates[0]
    return "mixed"


async def team_training_review(
    session, team_id: UUID, publication_id: UUID, rows: tuple[TrainingPublicationItemRow, ...]
) -> TeamTrainingReview:
    members = active_team_learners(team_id).subquery()
    cohort = (
        select(
            LearningPlan.owner_id.label("account_id"),
            func.greatest(LearningPlan.created_at, members.c.approved_at).label("since"),
        )
        .join(members, LearningPlan.owner_id == members.c.account_id)
        .where(LearningPlan.publication_id == publication_id)
        .subquery()
    )
    visible = {
        version: problem
        for version, problem in (
            await problems.learning_versions(session, {row.version_id for row in rows})
        ).items()
        if problem.available
    }
    totals = await team_training_verdict_totals(
        session, cohort, set(visible), as_of=datetime.now(UTC)
    )
    has_group = False
    items = []
    for row in rows:
        verdicts = totals.get(row.version_id, {})
        participants = sum(verdicts.values())
        if participants < _MIN_GROUP:
            continue
        has_group = True
        unresolved = participants - verdicts.get("accepted", 0)
        if unresolved >= _MIN_GROUP and 2 * unresolved >= participants:
            items.append(
                TeamTrainingReviewItem(
                    position=row.position,
                    title=visible[row.version_id].title,
                    topic=_shared_topic(verdicts),
                )
            )
    return TeamTrainingReview(
        state="ready" if has_group else "insufficient_data",
        items=tuple(items),
    )
