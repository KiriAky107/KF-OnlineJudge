"""Read public availability and fence actions that require a still-available version."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.modules.skills.publication_models import (
    SkillPublicationRow,
    SkillVersionArchiveRow,
    SkillVersionHoldRow,
)


def version_is_available():
    held = (
        select(SkillVersionHoldRow.version_id)
        .where(SkillVersionHoldRow.version_id == SkillPublicationRow.id)
        .exists()
    )
    return (SkillPublicationRow.state == "published") & ~held


def version_is_discoverable():
    archived = (
        select(SkillVersionArchiveRow.version_id)
        .where(SkillVersionArchiveRow.version_id == SkillPublicationRow.id)
        .exists()
    )
    return version_is_available() & ~archived


async def lock_available_version(db: AsyncSession, version_id: UUID) -> SkillPublicationRow | None:
    row = await db.scalar(
        select(SkillPublicationRow)
        .where(SkillPublicationRow.id == version_id, SkillPublicationRow.state == "published")
        .with_for_update(read=True)
    )
    # A hold creation takes the exclusive version lock. Check in a fresh snapshot
    # after acquiring the share lock so a hold committed while we waited is visible.
    if row is None or await db.get(SkillVersionHoldRow, version_id) is not None:
        return None
    return row


async def lock_discoverable_version(
    db: AsyncSession, version_id: UUID
) -> SkillPublicationRow | None:
    row = await lock_available_version(db, version_id)
    if row is None or await db.get(SkillVersionArchiveRow, version_id) is not None:
        return None
    return row
