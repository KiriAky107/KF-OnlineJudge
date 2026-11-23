"""Collect private reports on exact public versions for administrator review."""

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.skill_reports import (
    ReviewSkillReport,
    SkillReportPage,
    SkillReportReviewPage,
    SkillReportReviewView,
    SkillReportState,
    SkillReportView,
    SubmitSkillReport,
)
from kf_oj.modules.identity.public import AdministrationPort, Principal
from kf_oj.modules.skills.availability import lock_available_version
from kf_oj.modules.skills.drafts import SkillDraftConflict, SkillDraftUnavailable
from kf_oj.modules.skills.publication_models import SkillPublicationRow
from kf_oj.modules.skills.report_models import SkillReportRow


def _view(row: SkillReportRow) -> SkillReportView:
    return SkillReportView(
        report_id=row.id,
        version_id=row.version_id,
        reason=row.reason,
        details=row.details,
        state=row.state,
        submitted_at=row.created_at,
        decided_at=row.decided_at,
        decision_note=row.note,
    )


def _review(row: SkillReportRow, publication: SkillPublicationRow) -> SkillReportReviewView:
    return SkillReportReviewView(
        **_view(row).model_dump(),
        release_title=publication.document["title"],
        version=publication.version,
    )


def _retry(row: SkillReportRow, version_id: UUID, body: SubmitSkillReport) -> SkillReportView:
    if (row.version_id, row.reason, row.details) != (version_id, body.reason, body.details):
        raise SkillDraftConflict("report request describes different content")
    return _view(row)


class SkillReports:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], identity: AdministrationPort
    ) -> None:
        self._sessions, self._identity = sessions, identity

    async def submit(
        self, principal: Principal, version_id: UUID, body: SubmitSkillReport
    ) -> SkillReportView:
        if "\x00" in body.details:
            raise ValueError("invalid report text")
        request = select(SkillReportRow).where(
            SkillReportRow.reporter_id == principal.account_id,
            SkillReportRow.request_id == body.request_id,
        )
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            row = await db.scalar(request)
            if row is not None:
                return _retry(row, version_id, body)
            if await lock_available_version(db, version_id) is None:
                raise SkillDraftUnavailable("reported version unavailable")
            stored = await db.scalar(
                insert(SkillReportRow)
                .values(
                    id=uuid4(),
                    version_id=version_id,
                    reporter_id=principal.account_id,
                    request_id=body.request_id,
                    reason=body.reason,
                    details=body.details,
                )
                .on_conflict_do_nothing()
                .returning(SkillReportRow)
            )
            if stored is not None:
                return _view(stored)
            row = await db.scalar(request)
            if row is not None:
                return _retry(row, version_id, body)
            raise SkillDraftConflict("a report for this version is already pending")

    async def own(self, principal: Principal, version_id: UUID) -> SkillReportPage:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            rows = (
                await db.scalars(
                    select(SkillReportRow)
                    .where(
                        SkillReportRow.reporter_id == principal.account_id,
                        SkillReportRow.version_id == version_id,
                    )
                    .order_by(SkillReportRow.created_at.desc(), SkillReportRow.id)
                    .limit(21)
                )
            ).all()
            return SkillReportPage(
                items=tuple(_view(row) for row in rows[:20]), truncated=len(rows) > 20
            )

    async def reviews(self, principal: Principal, state: SkillReportState) -> SkillReportReviewPage:
        async with self._sessions.begin() as db:
            await self._identity.require_administrator(db, principal)
            order = (
                SkillReportRow.created_at.asc()
                if state == "pending"
                else SkillReportRow.decided_at.desc()
            )
            rows = (
                await db.execute(
                    select(SkillReportRow, SkillPublicationRow)
                    .join(SkillPublicationRow, SkillPublicationRow.id == SkillReportRow.version_id)
                    .where(SkillReportRow.state == state)
                    .order_by(order, SkillReportRow.id)
                    .limit(51)
                )
            ).all()
            return SkillReportReviewPage(
                items=tuple(_review(row, publication) for row, publication in rows[:50]),
                truncated=len(rows) > 50,
            )

    async def decide(
        self, principal: Principal, report_id: UUID, body: ReviewSkillReport
    ) -> SkillReportReviewView:
        if "\x00" in body.note:
            raise ValueError("invalid moderation note")
        async with self._sessions.begin() as db:
            await self._identity.require_administrator(db, principal)
            result = (
                await db.execute(
                    select(SkillReportRow, SkillPublicationRow)
                    .join(SkillPublicationRow, SkillPublicationRow.id == SkillReportRow.version_id)
                    .where(SkillReportRow.id == report_id)
                    .with_for_update(of=SkillReportRow)
                )
            ).one_or_none()
            if result is None:
                raise SkillDraftUnavailable("report unavailable")
            row, publication = result
            state = "confirmed" if body.action == "confirm" else "dismissed"
            if row.state != "pending":
                if (
                    row.state == state
                    and row.reviewer_id == principal.account_id
                    and row.note == body.note
                ):
                    return _review(row, publication)
                raise SkillDraftConflict("report already decided")
            row.decided_at = await db.scalar(select(func.clock_timestamp()))
            row.state = state
            row.reviewer_id = principal.account_id
            row.note = body.note
            return _review(row, publication)
