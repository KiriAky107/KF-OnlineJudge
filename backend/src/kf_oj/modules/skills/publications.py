"""Review fixed skill versions; never publish the current editable draft by reference."""

from uuid import UUID, uuid4

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.skill_drafts import SkillDraftDocument
from kf_oj.contracts.skill_publications import (
    DisableSkillVersion,
    PublicationState,
    PublishedSkill,
    PublishedSkillPage,
    ReviewSkillVersion,
    SkillVersionRating,
    SkillVersionReview,
    SkillVersionReviewPage,
    SubmitSkillVersion,
)
from kf_oj.modules.identity.public import AdministrationPort, Principal
from kf_oj.modules.skills.availability import lock_discoverable_version, version_is_discoverable
from kf_oj.modules.skills.draft_models import SkillDraftRow
from kf_oj.modules.skills.drafts import SkillDraftConflict, SkillDraftUnavailable
from kf_oj.modules.skills.publication_models import (
    SkillFavoriteRow,
    SkillPublicationRow,
    SkillRatingRow,
    SkillVersionArchiveRow,
    SkillVersionHoldRow,
)


def _review(
    row: SkillPublicationRow,
    hold: SkillVersionHoldRow | None = None,
    archive: SkillVersionArchiveRow | None = None,
) -> SkillVersionReview:
    state = row.state
    if archive is not None:
        state = "archived"
    if hold is not None:
        state = "disabled"
    return SkillVersionReview(
        draft_id=row.draft_id,
        draft_revision=row.draft_revision,
        version_id=row.id,
        version=row.version,
        based_on_version_id=row.based_on_version_id,
        author=row.author_name,
        content_digest=row.content_digest,
        document=SkillDraftDocument.model_validate(row.document),
        state=state,
        submitted_at=row.created_at,
        decided_at=row.decided_at,
        review_note=row.review_note,
        archived_at=archive.created_at if archive else None,
        disabled_at=hold.created_at if hold else None,
        disable_reason=hold.reason if hold else None,
    )


def _published(row: SkillPublicationRow, favorited: bool) -> PublishedSkill:
    assert row.decided_at is not None
    return PublishedSkill(
        skill_id=row.draft_id,
        version_id=row.id,
        version=row.version,
        based_on_version_id=row.based_on_version_id,
        author=row.author_name,
        content_digest=row.content_digest,
        document=SkillDraftDocument.model_validate(row.document),
        published_at=row.decided_at,
        favorited=favorited,
    )


def _check_public(document: SkillDraftDocument) -> None:
    if not document.license:
        raise ValueError("a license is required for sharing")
    if document.parameters.get("problem_ids"):
        raise ValueError("public defaults cannot contain personal problem references")


class SkillPublications:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], identity: AdministrationPort
    ) -> None:
        self._sessions, self._identity = sessions, identity

    async def _draft(
        self, db: AsyncSession, principal: Principal, draft_id: UUID, *, lock: bool = False
    ) -> SkillDraftRow:
        query = select(SkillDraftRow).where(
            SkillDraftRow.id == draft_id, SkillDraftRow.owner_id == principal.account_id
        )
        if lock:
            query = query.with_for_update()
        row = await db.scalar(query)
        if row is None:
            raise SkillDraftUnavailable("skill draft unavailable")
        return row

    @staticmethod
    async def _author_version(
        db: AsyncSession, principal: Principal, version_id: UUID
    ) -> SkillPublicationRow:
        row = await db.scalar(
            select(SkillPublicationRow)
            .where(
                SkillPublicationRow.id == version_id,
                SkillPublicationRow.owner_id == principal.account_id,
            )
            .with_for_update()
        )
        if row is None:
            raise SkillDraftUnavailable("skill version unavailable")
        return row

    @staticmethod
    async def _current_review(db: AsyncSession, row: SkillPublicationRow) -> SkillVersionReview:
        return _review(
            row,
            await db.get(SkillVersionHoldRow, row.id),
            await db.get(SkillVersionArchiveRow, row.id),
        )

    async def submit(
        self, principal: Principal, draft_id: UUID, body: SubmitSkillVersion
    ) -> SkillVersionReview:
        if not body.confirmed_public:
            raise ValueError("confirm that this declaration can be shared")
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            draft = await self._draft(db, principal, draft_id, lock=True)
            existing = await db.scalar(
                select(SkillPublicationRow).where(
                    SkillPublicationRow.draft_id == draft_id,
                    SkillPublicationRow.draft_revision == body.revision,
                )
            )
            if existing is not None:
                return await self._current_review(db, existing)
            if draft.revision != body.revision:
                raise SkillDraftConflict("save the current draft before submitting it")
            _check_public(SkillDraftDocument.model_validate(draft.document))
            count = await db.scalar(
                select(func.coalesce(func.max(SkillPublicationRow.version), 0)).where(
                    SkillPublicationRow.draft_id == draft_id
                )
            )
            row = SkillPublicationRow(
                id=uuid4(),
                draft_id=draft_id,
                owner_id=principal.account_id,
                author_name=principal.username,
                draft_revision=draft.revision,
                version=count + 1,
                based_on_version_id=draft.source_version_id,
                content_digest=draft.content_digest,
                document=draft.document,
            )
            db.add(row)
            await db.flush()
            return _review(row)

    async def author_versions(self, principal: Principal, draft_id: UUID) -> SkillVersionReviewPage:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            await self._draft(db, principal, draft_id)
            return await self._review_page(
                db, select(SkillPublicationRow).where(SkillPublicationRow.draft_id == draft_id)
            )

    async def withdraw(self, principal: Principal, version_id: UUID) -> SkillVersionReview:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            row = await self._author_version(db, principal, version_id)
            if row.state == "withdrawn":
                return _review(row)
            if row.state != "pending":
                raise SkillDraftConflict("only an undecided version can be withdrawn")
            row.decided_at = await db.scalar(select(func.clock_timestamp()))
            row.state = "withdrawn"
            return _review(row)

    async def archive(self, principal: Principal, version_id: UUID) -> SkillVersionReview:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            row = await self._author_version(db, principal, version_id)
            hold = await db.get(SkillVersionHoldRow, version_id)
            archive = await db.get(SkillVersionArchiveRow, version_id)
            if archive is not None:
                return _review(row, hold, archive)
            if row.state != "published" or hold is not None:
                raise SkillDraftConflict("only a published and available version can be archived")
            archive = SkillVersionArchiveRow(version_id=version_id)
            db.add(archive)
            await db.flush()
            return _review(row, archive=archive)

    async def pending(
        self, principal: Principal, state: PublicationState
    ) -> SkillVersionReviewPage:
        async with self._sessions.begin() as db:
            await self._identity.require_administrator(db, principal)
            query = select(SkillPublicationRow)
            if state == "disabled":
                query = query.where(SkillVersionHoldRow.version_id.is_not(None))
            else:
                query = query.where(
                    SkillPublicationRow.state == ("published" if state == "archived" else state),
                    SkillVersionHoldRow.version_id.is_(None),
                )
                if state == "archived":
                    query = query.where(SkillVersionArchiveRow.version_id.is_not(None))
                elif state == "published":
                    query = query.where(SkillVersionArchiveRow.version_id.is_(None))
            return await self._review_page(db, query, oldest_first=state == "pending")

    @staticmethod
    async def _review_page(
        db: AsyncSession,
        query: Select[tuple[SkillPublicationRow]],
        *,
        oldest_first: bool = False,
    ) -> SkillVersionReviewPage:
        created = SkillPublicationRow.created_at
        order = created.asc() if oldest_first else created.desc()
        rows = (
            await db.execute(
                query.add_columns(SkillVersionHoldRow, SkillVersionArchiveRow)
                .outerjoin(
                    SkillVersionHoldRow, SkillVersionHoldRow.version_id == SkillPublicationRow.id
                )
                .outerjoin(
                    SkillVersionArchiveRow,
                    SkillVersionArchiveRow.version_id == SkillPublicationRow.id,
                )
                .order_by(order, SkillPublicationRow.id)
                .limit(51)
            )
        ).all()
        return SkillVersionReviewPage(
            items=tuple(_review(row, hold, archive) for row, hold, archive in rows[:50]),
            truncated=len(rows) > 50,
        )

    async def decide(
        self, principal: Principal, version_id: UUID, body: ReviewSkillVersion
    ) -> SkillVersionReview:
        if "\x00" in body.note or (body.action == "reject" and not body.note):
            raise ValueError("a rejection requires a valid explanation")
        async with self._sessions.begin() as db:
            await self._identity.require_administrator(db, principal)
            row = await db.get(SkillPublicationRow, version_id, with_for_update=True)
            if row is None:
                raise SkillDraftUnavailable("skill version unavailable")
            result = "published" if body.action == "publish" else "rejected"
            if row.state != "pending":
                if (
                    row.state == result
                    and row.reviewer_id == principal.account_id
                    and row.review_note == body.note
                ):
                    return await self._current_review(db, row)
                raise SkillDraftConflict("the version already has a decision")
            if body.action == "publish":
                _check_public(SkillDraftDocument.model_validate(row.document))
            row.decided_at = await db.scalar(select(func.clock_timestamp()))
            row.state = result
            row.reviewer_id = principal.account_id
            row.review_note = body.note
            return _review(row)

    async def disable(
        self, principal: Principal, version_id: UUID, body: DisableSkillVersion
    ) -> SkillVersionReview:
        if "\x00" in body.reason:
            raise ValueError("invalid disable reason")
        async with self._sessions.begin() as db:
            await self._identity.require_administrator(db, principal)
            publication = await db.get(SkillPublicationRow, version_id, with_for_update=True)
            if publication is None or publication.state != "published":
                raise SkillDraftUnavailable("published version unavailable")
            hold = await db.get(SkillVersionHoldRow, version_id)
            archive = await db.get(SkillVersionArchiveRow, version_id)
            if hold is not None:
                if hold.operator_id == principal.account_id and hold.reason == body.reason:
                    return _review(publication, hold, archive)
                raise SkillDraftConflict("the version is already disabled")
            hold = SkillVersionHoldRow(
                version_id=version_id, operator_id=principal.account_id, reason=body.reason
            )
            db.add(hold)
            await db.flush()
            return _review(publication, hold, archive)

    @staticmethod
    def _favorited(principal: Principal):
        return (
            select(SkillFavoriteRow.owner_id)
            .where(
                SkillFavoriteRow.owner_id == principal.account_id,
                SkillFavoriteRow.skill_id == SkillPublicationRow.draft_id,
            )
            .exists()
        )

    async def discover(
        self,
        principal: Principal,
        title: str,
        purpose: AgentPurpose | None,
        favorites_only: bool = False,
    ) -> PublishedSkillPage:
        if "\x00" in title:
            raise ValueError("invalid search text")
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            favorited = self._favorited(principal)
            query = select(SkillPublicationRow, favorited).where(version_is_discoverable())
            if favorites_only:
                query = query.where(favorited)
            if title:
                query = query.where(
                    SkillPublicationRow.document["title"].astext.icontains(title, autoescape=True)
                )
            if purpose is not None:
                query = query.where(SkillPublicationRow.document["purpose"].astext == purpose.value)
            rows = (
                await db.execute(
                    query.order_by(
                        SkillPublicationRow.decided_at.desc(), SkillPublicationRow.id
                    ).limit(51)
                )
            ).all()
            return PublishedSkillPage(
                items=tuple(_published(row, saved) for row, saved in rows[:50]),
                truncated=len(rows) > 50,
            )

    async def published(self, principal: Principal, version_id: UUID) -> PublishedSkill:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            result = (
                await db.execute(
                    select(SkillPublicationRow, self._favorited(principal)).where(
                        SkillPublicationRow.id == version_id,
                        version_is_discoverable(),
                    )
                )
            ).one_or_none()
            if result is None:
                raise SkillDraftUnavailable("skill version unavailable")
            row, favorited = result
            return _published(row, favorited)

    async def set_favorite(self, principal: Principal, skill_id: UUID, favorited: bool) -> bool:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            if not favorited:
                await db.execute(
                    delete(SkillFavoriteRow).where(
                        SkillFavoriteRow.owner_id == principal.account_id,
                        SkillFavoriteRow.skill_id == skill_id,
                    )
                )
                return False
            version_id = await db.scalar(
                select(SkillPublicationRow.id)
                .where(
                    SkillPublicationRow.draft_id == skill_id,
                    version_is_discoverable(),
                )
                .order_by(SkillPublicationRow.version.desc())
                .limit(1)
                .with_for_update(read=True)
            )
            if version_id is None or await lock_discoverable_version(db, version_id) is None:
                raise SkillDraftUnavailable("skill unavailable")
            await db.execute(
                insert(SkillFavoriteRow)
                .values(owner_id=principal.account_id, skill_id=skill_id)
                .on_conflict_do_nothing()
            )
            return True

    @staticmethod
    async def _rating_summary(
        db: AsyncSession, principal: Principal, version_id: UUID
    ) -> SkillVersionRating:
        count, average, own = (
            await db.execute(
                select(
                    func.count(SkillRatingRow.score),
                    func.avg(SkillRatingRow.score),
                    func.max(SkillRatingRow.score).filter(
                        SkillRatingRow.owner_id == principal.account_id
                    ),
                ).where(SkillRatingRow.version_id == version_id)
            )
        ).one()
        return SkillVersionRating(
            version_id=version_id,
            count=count,
            average=float(round(average, 2)) if average is not None else None,
            own_score=own,
        )

    async def rating(self, principal: Principal, version_id: UUID) -> SkillVersionRating:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            await self._require_discoverable(db, version_id)
            return await self._rating_summary(db, principal, version_id)

    @staticmethod
    async def _require_discoverable(db: AsyncSession, version_id: UUID) -> None:
        if await lock_discoverable_version(db, version_id) is None:
            raise SkillDraftUnavailable("skill version unavailable")

    async def set_rating(
        self, principal: Principal, version_id: UUID, score: int | None
    ) -> SkillVersionRating:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            await self._require_discoverable(db, version_id)
            if score is None:
                await db.execute(
                    delete(SkillRatingRow).where(
                        SkillRatingRow.version_id == version_id,
                        SkillRatingRow.owner_id == principal.account_id,
                    )
                )
            else:
                saved = insert(SkillRatingRow).values(
                    version_id=version_id, owner_id=principal.account_id, score=score
                )
                await db.execute(
                    saved.on_conflict_do_update(
                        index_elements=(SkillRatingRow.version_id, SkillRatingRow.owner_id),
                        set_={"score": saved.excluded.score},
                        where=SkillRatingRow.score != saved.excluded.score,
                    )
                )
            return await self._rating_summary(db, principal, version_id)
