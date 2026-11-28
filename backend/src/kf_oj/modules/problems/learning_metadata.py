from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import array

from kf_oj.modules.problems.learning_models import ProblemDifficultyLevel, ProblemLearningMetadata
from kf_oj.modules.problems.models import Problem, ProblemVersion
from kf_oj.modules.problems.public import LearningCandidate


def query():
    return (
        select(
            Problem.id.label("problem_id"),
            Problem.slug,
            Problem.enabled,
            Problem.active_version_id,
            ProblemVersion.id.label("version_id"),
            ProblemVersion.title,
            ProblemVersion.published_at,
            ProblemLearningMetadata.tags,
            ProblemLearningMetadata.prerequisite_tags,
            ProblemLearningMetadata.difficulty,
            ProblemLearningMetadata.revision,
        )
        .join(ProblemVersion, Problem.id == ProblemVersion.problem_id)
        .outerjoin(
            ProblemLearningMetadata, ProblemLearningMetadata.problem_version_id == ProblemVersion.id
        )
    )


def project(row):
    return LearningCandidate(
        row.problem_id,
        row.version_id,
        row.title,
        tuple(row.tags or ()),
        row.difficulty,
        row.revision or 0,
        bool(row.enabled and row.published_at),
        row.slug,
        row.active_version_id == row.version_id,
        tuple(row.prerequisite_tags or ()),
        getattr(row, "difficulty_score", None),
    )


async def candidates(
    session,
    *,
    tags=(),
    difficulty=None,
    allowed_difficulties=(),
    match_any_tag=False,
    score_range: tuple[int | None, int | None] | None = None,
    limit=500,
):
    if not 1 <= limit <= 500:
        raise ValueError("invalid candidate limit")
    q = (
        query()
        .where(
            Problem.enabled.is_(True),
            Problem.active_version_id == ProblemVersion.id,
            ProblemVersion.published_at.is_not(None),
        )
        .order_by(Problem.id)
    )
    if tags:
        filters = ProblemLearningMetadata.tags
        q = q.where(
            filters.has_any(array(list(tags))) if match_any_tag else filters.contains(list(tags))
        )
    if difficulty is not None:
        q = q.where(ProblemLearningMetadata.difficulty == difficulty)
    if allowed_difficulties:
        q = q.where(ProblemLearningMetadata.difficulty.in_(allowed_difficulties))
    if score_range is not None:
        score = ProblemDifficultyLevel.score
        q = q.outerjoin(
            ProblemDifficultyLevel,
            ProblemDifficultyLevel.key
            == func.coalesce(
                ProblemLearningMetadata.difficulty_key, ProblemLearningMetadata.difficulty
            ),
        ).add_columns(score.label("difficulty_score"))
        minimum, maximum = score_range
        if minimum is not None:
            q = q.where(score >= minimum)
        if maximum is not None:
            q = q.where(score <= maximum)
        if minimum is not None or maximum is not None:
            q = q.order_by(None).order_by(score, Problem.id)
    rows = await session.execute(q.limit(limit + 1))
    return tuple(project(row) for row in rows)


async def versions(session, version_ids):
    if not version_ids:
        return {}
    rows = await session.execute(
        query().where(ProblemVersion.id.in_(version_ids), ProblemVersion.published_at.is_not(None))
    )
    return {row.version_id: project(row) for row in rows}


async def selected(session, version_ids, *, current_only: bool = True):
    if len(version_ids) > 10:
        raise ValueError("at most ten selected versions")
    if not version_ids:
        return {}
    # Pin availability and serialize with metadata updates to the chosen versions.
    selection = query().where(
        ProblemVersion.id.in_(version_ids),
        Problem.enabled.is_(True),
        ProblemVersion.published_at.is_not(None),
    )
    if current_only:
        selection = selection.where(Problem.active_version_id == ProblemVersion.id)
    rows = await session.execute(
        selection.order_by(Problem.id, ProblemVersion.id).with_for_update(
            read=True, of=(Problem, ProblemVersion)
        )
    )
    return {row.version_id: project(row) for row in rows}
