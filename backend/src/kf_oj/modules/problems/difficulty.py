from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from kf_oj.contracts.difficulty import DifficultyLevel, DifficultySettings
from kf_oj.modules.problems.learning_models import ProblemDifficultyLevel


def difficulty_view(row: ProblemDifficultyLevel) -> DifficultyLevel:
    return DifficultyLevel(
        key=row.key, label=row.label, score=row.score, color=row.color, tier=row.tier
    )


async def difficulty_levels(session, keys: set[str] | None = None) -> dict[str, DifficultyLevel]:
    statement = select(ProblemDifficultyLevel).order_by(
        ProblemDifficultyLevel.score, ProblemDifficultyLevel.key
    )
    if keys is not None:
        if not keys:
            return {}
        statement = statement.where(ProblemDifficultyLevel.key.in_(keys))
    return {row.key: difficulty_view(row) for row in await session.scalars(statement)}


async def save_difficulty(session, key: str, body: DifficultySettings) -> DifficultyLevel:
    values = body.model_dump()
    statement = insert(ProblemDifficultyLevel).values(key=key, **values)
    updated = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[ProblemDifficultyLevel.key],
            set_={field: values[field] for field in ("label", "score", "color")},
            where=ProblemDifficultyLevel.tier == body.tier,
        ).returning(ProblemDifficultyLevel)
    )
    if updated is None:
        raise ValueError("the tier of an existing difficulty level cannot change")
    return difficulty_view(updated)


async def select_difficulty_key(session, body, inherited=None) -> str | None:
    if body.difficulty_key is None:
        return (
            inherited.difficulty_key
            if inherited and inherited.difficulty == body.difficulty
            else None
        )
    level = await session.get(ProblemDifficultyLevel, body.difficulty_key)
    if level is None or level.tier != body.difficulty:
        raise ValueError("difficulty level does not match the selected tier")
    return level.key
