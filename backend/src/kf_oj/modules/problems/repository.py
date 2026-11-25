import hashlib
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.modules.problems.learning_models import ProblemLearningMetadata
from kf_oj.modules.problems.models import Problem, ProblemCase, ProblemVersion


async def lock_slug(session: AsyncSession, slug: str) -> None:
    # Two-key namespace is disjoint from the one-key artifact advisory locks.
    key = int.from_bytes(hashlib.sha256(slug.encode()).digest()[:4], signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(1262907468,:key)"), {"key": key})


async def lock_problem(session: AsyncSession, slug: str) -> Problem | None:
    return await session.scalar(select(Problem).where(Problem.slug == slug).with_for_update())


async def next_revision(session: AsyncSession, problem_id: UUID) -> int:
    latest = await session.scalar(
        select(func.max(ProblemVersion.revision)).where(ProblemVersion.problem_id == problem_id)
    )
    return (latest or 0) + 1


async def published_version(session: AsyncSession, version_id: UUID, *, admission=False):
    query = (
        select(ProblemVersion, Problem)
        .join(Problem, Problem.id == ProblemVersion.problem_id)
        .where(ProblemVersion.id == version_id, ProblemVersion.published_at.is_not(None))
    )
    if admission:
        # A new submission commits while this shared problem lock is held. A
        # concurrent disable either precedes admission or follows that commit.
        query = query.where(Problem.enabled.is_(True)).with_for_update(read=True, of=Problem)
    return (await session.execute(query)).one_or_none()


async def public_version(session: AsyncSession, version_id: UUID, *, admission=False):
    if admission:
        return await published_version(session, version_id, admission=True)
    return (
        await session.execute(
            select(ProblemVersion, Problem)
            .join(Problem, Problem.id == ProblemVersion.problem_id)
            .where(
                ProblemVersion.id == version_id,
                ProblemVersion.published_at.is_not(None),
                Problem.enabled.is_(True),
            )
        )
    ).one_or_none()


async def ordered_cases(session: AsyncSession, version_id: UUID):
    return list(
        await session.scalars(
            select(ProblemCase)
            .where(ProblemCase.version_id == version_id)
            .order_by(ProblemCase.position)
        )
    )


async def list_public(
    session: AsyncSession,
    after: str | None,
    limit: int,
    *,
    search=None,
    difficulty=None,
    difficulty_key=None,
    tag=None,
):
    query = (
        select(Problem, ProblemVersion, ProblemLearningMetadata)
        .join(ProblemVersion, ProblemVersion.id == Problem.active_version_id)
        .outerjoin(
            ProblemLearningMetadata, ProblemLearningMetadata.problem_version_id == ProblemVersion.id
        )
        .where(Problem.enabled.is_(True), ProblemVersion.published_at.is_not(None))
    )
    if after is not None:
        query = query.where(Problem.slug > after)
    if search:
        query = query.where(
            or_(
                Problem.slug.icontains(search, autoescape=True),
                ProblemVersion.title.icontains(search, autoescape=True),
            )
        )
    if difficulty is not None:
        query = query.where(ProblemLearningMetadata.difficulty == difficulty)
    if difficulty_key is not None:
        query = query.where(
            func.coalesce(
                ProblemLearningMetadata.difficulty_key, ProblemLearningMetadata.difficulty
            )
            == difficulty_key
        )
    if tag is not None:
        query = query.where(ProblemLearningMetadata.tags.contains([tag]))
    return list(await session.execute(query.order_by(Problem.slug).limit(limit)))
