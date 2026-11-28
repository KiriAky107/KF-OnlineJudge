"""Personal learning records confirmed from an owned diagnostic."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_api import (
    DiagnosticMistakeEvidence,
    MistakePractice,
    MistakePracticeItem,
    MistakePracticeRequest,
    PersonalMistake,
    PersonalMistakePage,
)
from kf_oj.contracts.agent_learning import FocusedTrainingParameters
from kf_oj.contracts.diagnostic import UbsanFinding
from kf_oj.contracts.learning import focused_exercise_count
from kf_oj.modules.contests.public import require_agent_access
from kf_oj.modules.identity.public import ActiveSessionPort, Principal
from kf_oj.modules.problems import public as problems
from kf_oj.runtime.storage import AgentRecordConflict, AgentRecordUnavailable

from .diagnostic_queue import DiagnosticQueue
from .mistake_models import PersonalMistakeRow
from .service import LearningService

_FOCUS = {
    "signed_integer_overflow": "按题面上下限估算中间值范围，并核对运算时实际使用的类型。",
    "division_by_zero": "列出能让除数为零的合法输入，并核对程序会进入的分支。",
    "index_out_of_bounds": "手算最小输入和最大合法下标，核对每处访问前的边界判断。",
    "invalid_shift": "检查操作数的位宽、符号和移位量，先手算位数的边界。",
}


def _evidence(row: PersonalMistakeRow) -> DiagnosticMistakeEvidence:
    return DiagnosticMistakeEvidence(
        request_id=row.request_id,
        submission_id=row.submission_id,
        problem_version_id=row.problem_version_id,
        sample_ordinal=row.sample_ordinal,
        finding=UbsanFinding.model_validate(
            {"category": row.category, "line": row.source_line, "column": row.source_column}
        ),
    )


def _record(row: PersonalMistakeRow) -> PersonalMistake:
    return PersonalMistake(
        **_evidence(row).model_dump(),
        note=row.note,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PersonalMistakes:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], identity: ActiveSessionPort
    ) -> None:
        self._sessions = sessions
        self._identity = identity

    async def _authorize(self, db: AsyncSession, principal: Principal) -> None:
        await self._identity.require_active_session(db, principal)
        await require_agent_access(db, principal.account_id)

    async def confirm(
        self, principal: Principal, queue: DiagnosticQueue, request_id: UUID, note: str
    ) -> PersonalMistake:
        async with self._sessions.begin() as db:
            evidence = await queue.mistake_evidence(db, principal, request_id)
            statement = (
                insert(PersonalMistakeRow)
                .values(
                    owner_id=principal.account_id,
                    request_id=evidence.request_id,
                    submission_id=evidence.submission_id,
                    problem_version_id=evidence.problem_version_id,
                    sample_ordinal=evidence.sample_ordinal,
                    category=evidence.finding.category,
                    source_line=evidence.finding.line,
                    source_column=evidence.finding.column,
                    note=note,
                )
                .on_conflict_do_nothing(index_elements=["owner_id", "request_id"])
                .returning(PersonalMistakeRow)
            )
            row = await db.scalar(statement)
            if row is None:
                row = await db.get(PersonalMistakeRow, (principal.account_id, request_id))
            if row is None:
                raise AgentRecordConflict("personal mistake changed during confirmation")
            return _record(row)

    async def read(self, principal: Principal, request_id: UUID) -> PersonalMistake:
        async with self._sessions.begin() as db:
            await self._authorize(db, principal)
            row = await db.get(PersonalMistakeRow, (principal.account_id, request_id))
            if row is None:
                raise AgentRecordUnavailable("personal mistake unavailable")
            return _record(row)

    async def list(
        self, principal: Principal, before: UUID | None, limit: int
    ) -> PersonalMistakePage:
        async with self._sessions.begin() as db:
            await self._authorize(db, principal)
            rows_for_owner = select(PersonalMistakeRow).where(
                PersonalMistakeRow.owner_id == principal.account_id
            )
            sort = (PersonalMistakeRow.created_at, PersonalMistakeRow.request_id)
            if before is not None:
                anchor = await db.get(PersonalMistakeRow, (principal.account_id, before))
                if anchor is None:
                    raise AgentRecordUnavailable("personal mistake cursor unavailable")
                rows_for_owner = rows_for_owner.where(
                    tuple_(*sort) < (anchor.created_at, anchor.request_id)
                )
            rows = (
                await db.scalars(
                    rows_for_owner.order_by(*(column.desc() for column in sort)).limit(limit + 1)
                )
            ).all()
            return PersonalMistakePage(
                items=tuple(_record(row) for row in rows[:limit]),
                next_before=rows[limit - 1].request_id if len(rows) > limit else None,
            )

    async def practice(
        self,
        principal: Principal,
        request: MistakePracticeRequest,
        authorize_tool: Callable[[AsyncSession], Awaitable[FocusedTrainingParameters]],
    ) -> MistakePractice:
        async with self._sessions.begin() as db:
            await self._authorize(db, principal)
            skill = await authorize_tool(db)
            query = select(PersonalMistakeRow).where(
                PersonalMistakeRow.owner_id == principal.account_id
            )
            if request.request_id is not None:
                query = query.where(PersonalMistakeRow.request_id == request.request_id)
            if request.submission_id is not None:
                query = query.where(PersonalMistakeRow.submission_id == request.submission_id)
            row = await db.scalar(
                query.order_by(
                    PersonalMistakeRow.created_at.desc(), PersonalMistakeRow.request_id.desc()
                )
                .limit(1)
                .with_for_update(read=True)
            )
            if row is None:
                raise AgentRecordUnavailable("confirmed mistake unavailable")
            source = (await problems.learning_versions(db, {row.problem_version_id})).get(
                row.problem_version_id
            )
            evidence = _evidence(row)
            minutes = request.available_minutes
            result = MistakePractice(
                evidence=evidence,
                focus=_FOCUS[evidence.finding.category],
                source_available=source is not None and source.available,
                source_title=source.title if source is not None and source.available else None,
                available_minutes=minutes,
                items=(),
                gaps=(),
            )
            if source is None or not source.available:
                return result.model_copy(update={"gaps": ("原题目版本当前不可使用。",)})
            if source.tags:
                count = focused_exercise_count(minutes) if minutes is not None else 5
                recommended = await LearningService(self._sessions).recommend_similar(
                    db, principal, source, count, skill
                )
                items = tuple(
                    MistakePracticeItem(
                        problem_version_id=item.problem_version_id,
                        title=item.title,
                        shared_tags=tuple(tag for tag in source.tags if tag in item.tags),
                    )
                    for item in recommended.items
                )
                result = result.model_copy(update={"items": items, "gaps": recommended.gaps})
            else:
                result = result.model_copy(update={"gaps": ("原题目版本尚未标注知识点。",)})
            # Recheck a scheduled contest that may have started during the catalog query.
            await require_agent_access(db, principal.account_id)
            if minutes is None:
                return result
            if not result.items:
                return result.model_copy(
                    update={
                        "review_minutes": minutes // 2,
                        "verification_minutes": minutes - minutes // 2,
                    }
                )
            check_minutes = max(1, minutes // 6)
            each, remainder = divmod(minutes - 2 * check_minutes, len(result.items))
            return result.model_copy(
                update={
                    "review_minutes": check_minutes,
                    "verification_minutes": check_minutes,
                    "items": tuple(
                        item.model_copy(update={"estimated_minutes": each + (index < remainder)})
                        for index, item in enumerate(result.items)
                    ),
                }
            )

    async def edit(self, principal: Principal, request_id: UUID, note: str) -> PersonalMistake:
        async with self._sessions.begin() as db:
            await self._authorize(db, principal)
            row = await db.scalar(
                update(PersonalMistakeRow)
                .where(
                    PersonalMistakeRow.owner_id == principal.account_id,
                    PersonalMistakeRow.request_id == request_id,
                )
                .values(
                    note=note,
                    updated_at=func.greatest(PersonalMistakeRow.updated_at, func.clock_timestamp()),
                )
                .returning(PersonalMistakeRow)
            )
            if row is None:
                raise AgentRecordUnavailable("personal mistake unavailable")
            return _record(row)

    async def withdraw(self, principal: Principal, request_id: UUID) -> None:
        async with self._sessions.begin() as db:
            await self._authorize(db, principal)
            await db.execute(
                delete(PersonalMistakeRow).where(
                    PersonalMistakeRow.owner_id == principal.account_id,
                    PersonalMistakeRow.request_id == request_id,
                )
            )
