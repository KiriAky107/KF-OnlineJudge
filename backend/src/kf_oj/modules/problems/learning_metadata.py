from sqlalchemy import select

from kf_oj.modules.problems.learning_models import ProblemLearningMetadata
from kf_oj.modules.problems.models import Problem, ProblemVersion
from kf_oj.modules.problems.public import LearningCandidate


def query():
    return (
        select(Problem, ProblemVersion, ProblemLearningMetadata)
        .join(ProblemVersion, Problem.id == ProblemVersion.problem_id)
        .outerjoin(
            ProblemLearningMetadata, ProblemLearningMetadata.problem_version_id == ProblemVersion.id
        )
    )


def project(problem, version, metadata):
    return LearningCandidate(
        problem.id,
        version.id,
        version.title,
        tuple(metadata.tags) if metadata else (),
        metadata.difficulty if metadata else None,
        metadata.revision if metadata else 0,
        bool(problem.enabled and version.published_at),
        problem.slug,
        problem.active_version_id == version.id,
    )


async def candidates(session, *, tags=(), difficulty=None, limit=500):
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
        q = q.where(ProblemLearningMetadata.tags.contains(list(tags)))
    if difficulty is not None:
        q = q.where(ProblemLearningMetadata.difficulty == difficulty)
    rows = await session.execute(q.limit(limit + 1))
    return tuple(project(*row) for row in rows)


async def versions(session, version_ids):
    if not version_ids:
        return {}
    rows = await session.execute(
        query().where(ProblemVersion.id.in_(version_ids), ProblemVersion.published_at.is_not(None))
    )
    return {row[1].id: project(*row) for row in rows}


async def selected(session, version_ids):
    if len(version_ids) > 10:
        raise ValueError("at most ten selected versions")
    if not version_ids:
        return {}
    # Lock only chosen logical problems against concurrent disable/publication.
    # Metadata is captured as a revisioned snapshot, not modified by plan creation.
    rows = await session.execute(
        query()
        .where(
            ProblemVersion.id.in_(version_ids),
            Problem.active_version_id == ProblemVersion.id,
            Problem.enabled.is_(True),
            ProblemVersion.published_at.is_not(None),
        )
        .order_by(Problem.id)
        .with_for_update(read=True, of=Problem)
    )
    return {row[1].id: project(*row) for row in rows}
