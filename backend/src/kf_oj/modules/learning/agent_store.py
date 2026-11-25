"""PostgreSQL implementation of the private learning Agent storage port."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Uuid, and_, case, delete, extract, func, literal, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_api import (
    AgentConversationPage,
    AgentConversationRun,
    AgentHistoryItem,
    AgentHistoryPage,
    AgentRunTrace,
    AgentTraceStep,
    ConversationMessage,
    ConversationPage,
)
from kf_oj.contracts.agent_learning import (
    LEARNING_RESULT,
    AgentExecution,
    AgentLearningResult,
    RunFeedback,
    StoredAgentLearningInput,
    TrainingSetDraft,
    UpdateRunFeedback,
)
from kf_oj.contracts.agent_runtime import AgentPurpose, AgentRun, AgentSession, AgentStep, RunState
from kf_oj.contracts.learning_model import (
    MODEL_REQUEST_SECONDS,
    ModelOutputChannel,
    ModelTokenUsage,
)
from kf_oj.modules.identity.public import AcceptedSession
from kf_oj.modules.learning.agent_model_budget import (
    AgentModelLimits,
    finish_model_attempt,
    reserve_model_attempt,
)
from kf_oj.modules.learning.agent_models import (
    AgentCheckpointRow,
    AgentConversationSessionRow,
    AgentFeedbackRow,
    AgentModelAttemptRow,
    AgentPartialAnswerRow,
    AgentPlanLinkRow,
    AgentRunRow,
    AgentSessionRow,
    AgentSourceExcerptRow,
    AgentStepRow,
    AgentTaskRow,
    AgentTimingRow,
    AgentWorkflowRow,
)
from kf_oj.modules.learning.agent_tool_models import AgentToolAttemptRow
from kf_oj.modules.learning.models import HintUsage
from kf_oj.runtime.state import check_run_transition
from kf_oj.runtime.storage import AgentRecordConflict, AgentRecordUnavailable


class LearningQueueBusy(ValueError):
    """Admission limits for active queued work."""


def _conversation_question():
    data = AgentWorkflowRow.input
    return func.coalesce(
        data["chat"]["question"].as_string(),
        data["training_set"]["message"].as_string(),
        data["review_question"].as_string(),
        data["periodic_review"]["question"].as_string(),
    )


def _session_record(row: AgentSessionRow) -> AgentSession:
    return AgentSession(
        session_id=row.id, owner_id=row.owner_id, purpose=row.purpose, created_at=row.created_at
    )


def _run_record(row: AgentRunRow) -> AgentRun:
    return AgentRun(
        run_id=row.id,
        session_id=row.session_id,
        request_id=row.request_id,
        skill_version_id=row.skill_version_id,
        configuration_digest=row.configuration_digest,
        input_digest=row.input_digest,
        state=row.state,
        revision=row.revision,
        created_at=row.created_at,
    )


def _step_record(row: AgentStepRow) -> AgentStep:
    return AgentStep(
        step_id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        kind=row.kind,
        invocation_version=row.invocation_version,
        arguments_digest=row.arguments_digest,
        state=row.state,
        result_digest=row.result_digest,
        error_code=row.error_code,
    )


async def begin_timing(db: AsyncSession, run_id: UUID, started: datetime) -> None:
    await db.execute(
        insert(AgentTimingRow)
        .values(run_id=run_id, first_claimed_at=started)
        .on_conflict_do_nothing()
    )


async def finish_timing(db: AsyncSession, run_id: UUID) -> None:
    written = insert(AgentTimingRow).values(run_id=run_id, finished_at=func.clock_timestamp())
    await db.execute(
        written.on_conflict_do_update(
            index_elements=[AgentTimingRow.run_id],
            set_={"finished_at": written.excluded.finished_at},
            where=AgentTimingRow.finished_at.is_(None),
        )
    )


async def clear_partial_answer(db: AsyncSession, run_id: UUID) -> None:
    await db.execute(delete(AgentPartialAnswerRow).where(AgentPartialAnswerRow.run_id == run_id))


def _duration_ms(start: datetime, stop: datetime | None) -> int | None:
    return None if stop is None else max(0, int((stop - start).total_seconds() * 1000))


class PostgresAgentStorage:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], owner_id: UUID) -> None:
        self._sessions = sessions
        self._owner_id = owner_id

    def _owned_sessions(self):
        return select(AgentSessionRow).where(AgentSessionRow.owner_id == self._owner_id)

    def _owned_runs(self):
        return (
            select(AgentRunRow)
            .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
            .where(AgentSessionRow.owner_id == self._owner_id)
        )

    async def _session_row(self, db: AsyncSession, session_id: UUID) -> AgentSessionRow:
        row = await db.scalar(self._owned_sessions().where(AgentSessionRow.id == session_id))
        if row is None:
            raise AgentRecordUnavailable("Agent record unavailable")
        return row

    async def _conversation_root(self, db: AsyncSession, session_id: UUID) -> UUID:
        root = await db.scalar(
            select(AgentConversationSessionRow.root_session_id).where(
                AgentConversationSessionRow.session_id == session_id
            )
        )
        return root or session_id

    async def create_session(
        self, purpose: AgentPurpose, *, conversation_session_id: UUID | None = None
    ) -> AgentSession:
        record = AgentSession(
            session_id=uuid4(),
            owner_id=self._owner_id,
            purpose=purpose,
            created_at=datetime.now(UTC),
        )
        async with self._sessions.begin() as db:
            root_id = None
            if conversation_session_id is not None:
                await self._session_row(db, conversation_session_id)
                root_id = await self._conversation_root(db, conversation_session_id)
            db.add(
                AgentSessionRow(
                    id=record.session_id,
                    owner_id=record.owner_id,
                    purpose=record.purpose,
                    created_at=record.created_at,
                )
            )
            if root_id is not None:
                await db.flush()
                db.add(
                    AgentConversationSessionRow(
                        session_id=record.session_id, root_session_id=root_id
                    )
                )
        return record

    async def get_session(self, session_id: UUID) -> AgentSession:
        async with self._sessions() as db:
            return _session_record(await self._session_row(db, session_id))

    async def _admit_workflow(self, db: AsyncSession, session: AgentSessionRow, request_id: UUID):
        await db.execute(text("SELECT pg_advisory_xact_lock(1262907472, 0)"))
        existing = await db.scalar(
            select(AgentRunRow.id).where(
                AgentRunRow.session_id == session.id,
                AgentRunRow.request_id == request_id,
            )
        )
        if existing is not None:
            return
        root_id = await self._conversation_root(db, session.id)
        has_linked_session = (
            select(AgentConversationSessionRow.session_id)
            .where(AgentConversationSessionRow.root_session_id == root_id)
            .correlate(None)
            .exists()
        )
        queued = (
            select(
                func.count(),
                func.count().filter(AgentSessionRow.owner_id == self._owner_id),
                func.count().filter(
                    AgentSessionRow.owner_id == self._owner_id,
                    or_(
                        AgentSessionRow.id == root_id,
                        AgentConversationSessionRow.root_session_id == root_id,
                    ),
                ),
                has_linked_session,
            )
            .select_from(AgentTaskRow)
            .join(AgentRunRow, AgentRunRow.id == AgentTaskRow.run_id)
            .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
            .outerjoin(
                AgentConversationSessionRow,
                AgentConversationSessionRow.session_id == AgentSessionRow.id,
            )
            .where(AgentRunRow.state.in_(("queued", "running", "retry_wait", "cancel_requested")))
        )
        all_active, my_active, conversation_active, linked = (await db.execute(queued)).one()
        if linked and conversation_active:
            raise AgentRecordConflict("conversation already has an active turn")
        if all_active >= 200 or my_active >= 4:
            raise LearningQueueBusy("learning queue capacity exhausted")

    async def enqueue(
        self,
        session_id: UUID,
        *,
        request_id: UUID,
        skill_version_id: UUID,
        configuration_digest: str,
        input_digest: str,
        workflow_input: StoredAgentLearningInput | None = None,
        receipt: AcceptedSession | None = None,
        source_excerpt: bytes | None = None,
    ) -> AgentRun:
        if (workflow_input is None) != (receipt is None):
            raise ValueError("a queued learning workflow requires a session receipt")
        if (source_excerpt is not None) != (
            workflow_input is not None and workflow_input.model_source_excerpt is not None
        ):
            raise ValueError("source excerpt requires matching workflow consent")
        async with self._sessions.begin() as db:
            session = await self._session_row(db, session_id)
            if receipt is not None:
                await self._admit_workflow(db, session, request_id)
            request = AgentRun(
                run_id=uuid4(),
                session_id=session_id,
                request_id=request_id,
                skill_version_id=skill_version_id,
                configuration_digest=configuration_digest,
                input_digest=input_digest,
                created_at=datetime.now(UTC),
            )
            pinned = request.model_dump(
                include={"skill_version_id", "configuration_digest", "input_digest"}
            )
            row = await db.scalar(
                insert(AgentRunRow)
                .values(id=request.run_id, session_id=session_id, request_id=request_id, **pinned)
                .on_conflict_do_nothing(constraint="uq_learning_agent_run_request")
                .returning(AgentRunRow)
            )
            if row is None:
                row = (
                    await db.scalars(
                        self._owned_runs().where(
                            AgentRunRow.session_id == session_id,
                            AgentRunRow.request_id == request_id,
                        )
                    )
                ).one()
                if any(getattr(row, name) != value for name, value in pinned.items()):
                    raise AgentRecordConflict("Agent request already has different inputs")
            elif workflow_input is not None:
                assert receipt is not None
                await db.execute(
                    insert(AgentWorkflowRow).values(
                        run_id=row.id, input=workflow_input.model_dump(mode="json")
                    )
                )
                db.add(
                    AgentTaskRow(
                        run_id=row.id,
                        authorization_version=receipt.authorization_version,
                        session_proof=receipt.session_proof,
                    )
                )
                if source_excerpt is not None:
                    db.add(AgentSourceExcerptRow(run_id=row.id, source_utf8=source_excerpt))
            return _run_record(row)

    async def get_run(self, run_id: UUID) -> AgentRun:
        async with self._sessions() as db:
            row = await db.scalar(self._owned_runs().where(AgentRunRow.id == run_id))
            if row is None:
                raise AgentRecordUnavailable("Agent record unavailable")
            return _run_record(row)

    async def history(
        self,
        *,
        group_chat: bool = False,
        problem_version_id: UUID | None = None,
        training_drafts: bool = False,
    ) -> AgentHistoryPage:
        query = (
            self._owned_runs()
            .join(AgentTaskRow, AgentTaskRow.run_id == AgentRunRow.id)
            .with_only_columns(
                AgentRunRow.id.label("run_id"),
                AgentRunRow.session_id,
                AgentRunRow.skill_version_id,
                AgentSessionRow.purpose,
                AgentRunRow.state,
                AgentRunRow.created_at,
            )
        )
        if training_drafts or group_chat or problem_version_id is not None:
            query = query.join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            if training_drafts:
                title = AgentWorkflowRow.input["training_set"]["title"].as_string()
                query = query.where(
                    AgentSessionRow.purpose == AgentPurpose.FOCUSED_TRAINING,
                    title.is_not(None),
                )
                partition = (AgentRunRow.session_id,)
            else:
                chat = AgentWorkflowRow.input["chat"]
                question = chat["question"].as_string()
                if group_chat:
                    query = query.where(
                        AgentWorkflowRow.input["training_set"]["title"].as_string().is_(None)
                    )
                if problem_version_id is not None:
                    query = query.where(
                        chat["problem_version_id"].as_string() == str(problem_version_id)
                    )
                partition = (
                    question.is_not(None),
                    case((question.is_not(None), AgentRunRow.session_id), else_=AgentRunRow.id),
                )
                title = func.first_value(func.left(question, 80)).over(
                    partition_by=partition, order_by=(AgentRunRow.created_at, AgentRunRow.id)
                )
            rank = func.row_number().over(
                partition_by=partition,
                order_by=(AgentRunRow.created_at.desc(), AgentRunRow.id.desc()),
            )
            ranked = query.add_columns(title.label("conversation_title"), rank.label("position"))
            latest = ranked.subquery()
            query = select(*[col for col in latest.c if col.key != "position"]).where(
                latest.c.position == 1
            )
            if training_drafts:
                query = query.outerjoin(
                    AgentPlanLinkRow, AgentPlanLinkRow.run_id == latest.c.run_id
                ).add_columns(AgentPlanLinkRow.plan_id.label("training_id"))
            order = (latest.c.created_at.desc(), latest.c.run_id.desc())
        else:
            order = (AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        async with self._sessions() as db:
            found = (await db.execute(query.order_by(*order).limit(31))).mappings().all()
        return AgentHistoryPage(
            items=tuple(AgentHistoryItem.model_validate(row) for row in found[:30]),
            truncated=len(found) > 30,
        )

    async def recent_conversations(
        self, problem_version_id: UUID | None = None
    ) -> AgentHistoryPage:
        root = func.coalesce(AgentConversationSessionRow.root_session_id, AgentRunRow.session_id)
        position = (AgentRunRow.created_at, AgentRunRow.id)
        training_title = AgentWorkflowRow.input["training_set"]["title"].as_string()
        title = func.coalesce(_conversation_question(), training_title)
        newest = func.row_number().over(
            partition_by=root, order_by=tuple(column.desc() for column in position)
        )
        first_title = func.first_value(func.left(title, 80)).over(
            partition_by=root, order_by=(title.is_(None), *position)
        )
        ranked = (
            self._owned_runs()
            .join(AgentTaskRow, AgentTaskRow.run_id == AgentRunRow.id)
            .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            .outerjoin(
                AgentConversationSessionRow,
                AgentConversationSessionRow.session_id == AgentRunRow.session_id,
            )
            .outerjoin(AgentPlanLinkRow, AgentPlanLinkRow.run_id == AgentRunRow.id)
            .with_only_columns(
                AgentRunRow.id.label("run_id"),
                AgentRunRow.session_id,
                AgentRunRow.skill_version_id,
                AgentSessionRow.purpose,
                AgentRunRow.state,
                AgentRunRow.created_at,
                root.label("conversation_id"),
                first_title.label("conversation_title"),
                training_title.is_not(None).label("has_training_request"),
                AgentPlanLinkRow.plan_id.label("training_id"),
                newest.label("position"),
            )
        )
        if problem_version_id is not None:
            matches_problem = AgentWorkflowRow.input["chat"][
                "problem_version_id"
            ].as_string() == str(problem_version_id)
            ranked = ranked.add_columns(
                func.bool_or(matches_problem).over(partition_by=root).label("contains_problem")
            )
        latest = ranked.subquery()
        query = select(
            *(column for column in latest.c if column.key not in {"position", "contains_problem"})
        ).where(latest.c.position == 1)
        if problem_version_id is not None:
            query = query.where(latest.c.contains_problem.is_(True))
        async with self._sessions() as db:
            rows = (
                (
                    await db.execute(
                        query.order_by(latest.c.created_at.desc(), latest.c.run_id.desc()).limit(31)
                    )
                )
                .mappings()
                .all()
            )
        return AgentHistoryPage(
            items=tuple(AgentHistoryItem.model_validate(row) for row in rows[:30]),
            truncated=len(rows) > 30,
        )

    async def trace(self, run_id: UUID) -> AgentRunTrace:
        async with self._sessions() as db:
            found = (
                await db.execute(
                    self._owned_runs()
                    .join(AgentTaskRow, AgentTaskRow.run_id == AgentRunRow.id)
                    .outerjoin(AgentTimingRow, AgentTimingRow.run_id == AgentTaskRow.run_id)
                    .outerjoin(AgentModelAttemptRow, AgentModelAttemptRow.run_id == AgentRunRow.id)
                    .add_columns(
                        AgentTaskRow.attempts,
                        AgentTimingRow,
                        AgentModelAttemptRow.started_at,
                        AgentModelAttemptRow.finished_at,
                        AgentModelAttemptRow.reported_usage,
                    )
                    .where(AgentRunRow.id == run_id)
                )
            ).one_or_none()
            if found is None:
                raise AgentRecordUnavailable("Agent workflow unavailable")
            run, attempts, timing, model_started, model_finished, reported_usage = found
            calls, finished, seconds = (
                await db.execute(
                    select(
                        func.count(),
                        func.count(AgentToolAttemptRow.finished_at),
                        func.coalesce(
                            func.sum(
                                extract(
                                    "epoch",
                                    AgentToolAttemptRow.finished_at
                                    - AgentToolAttemptRow.started_at,
                                )
                            ),
                            0,
                        ),
                    ).where(AgentToolAttemptRow.run_id == run.id)
                )
            ).one()
            latest = (
                await db.scalars(
                    select(AgentStepRow)
                    .where(AgentStepRow.run_id == run.id)
                    .order_by(AgentStepRow.sequence.desc())
                    .limit(13)
                )
            ).all()
            return AgentRunTrace(
                run_id=run.id,
                attempts=attempts,
                queue_ms=_duration_ms(run.created_at, timing.first_claimed_at if timing else None),
                duration_ms=_duration_ms(run.created_at, timing.finished_at if timing else None),
                tool_calls=calls,
                finished_tool_calls=finished,
                tool_elapsed_ms=max(0, int(seconds * 1000)),
                model_elapsed_ms=None
                if model_started is None
                else _duration_ms(model_started, model_finished),
                model_usage=ModelTokenUsage.model_validate(reported_usage)
                if reported_usage is not None
                else None,
                steps=tuple(
                    AgentTraceStep(
                        sequence=step.sequence,
                        kind=step.kind,
                        state=step.state,
                        invocation_version=step.invocation_version,
                        error_code=step.error_code,
                    )
                    for step in reversed(latest[:12])
                ),
                truncated=len(latest) > 12,
            )

    async def find_request(self, session_id: UUID, request_id: UUID) -> AgentRun | None:
        async with self._sessions() as db:
            row = await db.scalar(
                self._owned_runs().where(
                    AgentRunRow.session_id == session_id, AgentRunRow.request_id == request_id
                )
            )
            return None if row is None else _run_record(row)

    async def workflow_input(self, running: AgentRun) -> StoredAgentLearningInput:
        async with self._sessions() as db:
            row = await db.scalar(
                select(AgentWorkflowRow).where(
                    AgentWorkflowRow.run_id.in_(
                        self._owned_runs()
                        .with_only_columns(AgentRunRow.id)
                        .where(
                            AgentRunRow.id == running.run_id,
                            AgentRunRow.revision == running.revision,
                            AgentRunRow.state == RunState.RUNNING.value,
                        )
                    )
                )
            )
            if row is None:
                raise AgentRecordUnavailable("Agent workflow unavailable")
            return StoredAgentLearningInput.model_validate(row.input)

    async def conversation_runs(
        self, db: AsyncSession, session_id: UUID, before: UUID | None, limit: int
    ) -> AgentConversationPage:
        await self._session_row(db, session_id)
        root_id = await self._conversation_root(db, session_id)
        owned = (
            self._owned_runs()
            .outerjoin(
                AgentConversationSessionRow,
                AgentConversationSessionRow.session_id == AgentSessionRow.id,
            )
            .where(
                or_(
                    AgentSessionRow.id == root_id,
                    AgentConversationSessionRow.root_session_id == root_id,
                )
            )
        )
        position = (AgentRunRow.created_at, AgentRunRow.id)
        if before is not None:
            cursor = (
                await db.execute(owned.with_only_columns(*position).where(AgentRunRow.id == before))
            ).one_or_none()
            if cursor is None:
                raise AgentRecordUnavailable("conversation cursor unavailable")
            owned = owned.where(tuple_(*position) < tuple_(*cursor))
        training = AgentWorkflowRow.input["training_set"]
        query = (
            owned.join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            .outerjoin(AgentPlanLinkRow, AgentPlanLinkRow.run_id == AgentRunRow.id)
            .with_only_columns(
                AgentRunRow.id.label("run_id"),
                AgentRunRow.session_id,
                AgentSessionRow.purpose,
                AgentRunRow.state,
                AgentRunRow.created_at,
                _conversation_question().label("question"),
                AgentWorkflowRow.result["model_hint"]["text"].as_string().label("answer"),
                AgentWorkflowRow.result["model_hint"]["reasoning"].as_string().label("reasoning"),
                training.label("training_request"),
                AgentPlanLinkRow.plan_id.label("training_id"),
            )
            .order_by(*(column.desc() for column in position))
            .limit(limit + 1)
        )
        rows = (await db.execute(query)).mappings().all()
        return AgentConversationPage(
            conversation_id=root_id,
            runs=tuple(AgentConversationRun.model_validate(row) for row in reversed(rows[:limit])),
            next_before=rows[limit - 1]["run_id"] if len(rows) > limit else None,
        )

    async def conversation(self, db: AsyncSession, before: UUID, limit: int) -> ConversationPage:
        chat = AgentWorkflowRow.input["chat"]
        training = AgentWorkflowRow.input["training_set"]
        question = func.coalesce(chat["question"].as_string(), training["message"].as_string())
        parent = func.coalesce(
            chat["previous_run_id"].as_string(),
            training["revision"]["run_id"].as_string(),
            training["previous_run_id"].as_string(),
        ).cast(Uuid)
        has_conversation = question.is_not(None) | training["title"].as_string().is_not(None)
        seed = (
            self._owned_runs()
            .with_only_columns(
                AgentRunRow.id.label("run_id"),
                AgentRunRow.session_id,
                parent.label("parent_id"),
                literal(0).label("depth"),
            )
            .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            .where(AgentRunRow.id == before, has_conversation)
            .cte("conversation", recursive=True)
        )
        # Stay inside the owned session and fetch one extra parent to determine pagination.
        thread = seed.union_all(
            select(AgentRunRow.id, AgentRunRow.session_id, parent, seed.c.depth + 1)
            .select_from(AgentRunRow)
            .join(
                seed,
                and_(
                    AgentRunRow.id == seed.c.parent_id, AgentRunRow.session_id == seed.c.session_id
                ),
            )
            .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            .where(seed.c.depth <= limit, has_conversation)
        )
        rows = (
            await db.execute(
                select(
                    thread.c.run_id,
                    AgentRunRow.state,
                    question.label("question"),
                    training.label("training_request"),
                    AgentWorkflowRow.result["model_hint"]["text"].as_string().label("answer"),
                )
                .select_from(thread)
                .join(AgentRunRow, AgentRunRow.id == thread.c.run_id)
                .join(AgentWorkflowRow, AgentWorkflowRow.run_id == thread.c.run_id)
                .order_by(thread.c.depth)
            )
        ).all()
        if not rows:
            raise AgentRecordUnavailable("conversation unavailable")
        page = rows[1 : limit + 1]
        return ConversationPage(
            messages=tuple(
                ConversationMessage(
                    run_id=row.run_id,
                    question=row.question,
                    answer=row.answer,
                    state=RunState(row.state),
                    training_request=(
                        TrainingSetDraft.model_validate(row.training_request)
                        if row.training_request is not None
                        else None
                    ),
                )
                for row in reversed(page)
            ),
            next_before=page[-1].run_id if len(rows) > limit + 1 else None,
        )

    async def execution(self, run_id: UUID) -> AgentExecution:
        async with self._sessions() as db:
            return await self.execution_in(db, run_id)

    async def execution_result(self, run_id: UUID) -> AgentExecution:
        async with self._sessions() as db:
            query = (
                self._owned_runs()
                .add_columns(AgentWorkflowRow.result)
                .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
                .where(AgentRunRow.id == run_id)
            )
            found = (await db.execute(query)).one_or_none()
            if found is None:
                raise AgentRecordUnavailable("Agent workflow unavailable")
            run, result = found
            return AgentExecution(
                run=_run_record(run),
                result=None if result is None else LEARNING_RESULT.validate_python(result),
            )

    async def execution_in(
        self, db: AsyncSession, run_id: UUID, *, lock: bool = False
    ) -> AgentExecution:
        query = (
            self._owned_runs()
            .add_columns(
                AgentWorkflowRow,
                AgentFeedbackRow,
                AgentPartialAnswerRow.text,
                AgentPartialAnswerRow.reasoning,
            )
            .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            .outerjoin(AgentFeedbackRow, AgentFeedbackRow.run_id == AgentRunRow.id)
            .outerjoin(AgentPartialAnswerRow, AgentPartialAnswerRow.run_id == AgentRunRow.id)
            .where(AgentRunRow.id == run_id)
        )
        if lock:
            query = query.with_for_update(of=AgentRunRow)
        found = (await db.execute(query)).one_or_none()
        if found is None:
            raise AgentRecordUnavailable("Agent workflow unavailable")
        run, workflow, feedback, partial, partial_reasoning = found
        body = (
            StoredAgentLearningInput.model_validate(workflow.input)
            if workflow.input.get("chat") is not None
            else None
        )
        show_preview = (
            body is not None
            and body.chat is not None
            and body.chat.bounded_hint_level() is None
            and run.state == RunState.RUNNING
        )
        return AgentExecution(
            run=_run_record(run),
            result=None
            if workflow.result is None
            else LEARNING_RESULT.validate_python(workflow.result),
            failure_code=workflow.failure_code,
            training_request=(
                TrainingSetDraft.model_validate(request)
                if (request := workflow.input.get("training_set")) is not None
                else None
            ),
            chat=body.chat if body else None,
            review_question=workflow.input.get("review_question"),
            periodic_review=workflow.input.get("periodic_review"),
            partial_answer=partial if show_preview else None,
            partial_reasoning=partial_reasoning if show_preview else None,
            chat_history=body.chat_context.history if body and body.chat_context else (),
            chat_private_turn=bool(
                body and body.chat and (body.chat.include_submission_details or body.chat_source)
            ),
            feedback=None
            if feedback is None
            else RunFeedback.model_validate(feedback, from_attributes=True),
        )

    async def save_feedback(
        self, db: AsyncSession, run_id: UUID, body: UpdateRunFeedback
    ) -> AgentExecution:
        execution = await self.execution_in(db, run_id, lock=True)
        if execution.run.state != RunState.SUCCEEDED or execution.result is None:
            raise AgentRecordConflict("learning feedback requires a completed result")
        row = await db.get(AgentFeedbackRow, run_id, populate_existing=True)
        if body.rating is None:
            if row is not None:
                await db.delete(row)
            return execution.model_copy(update={"feedback": None})
        if row is None:
            row = AgentFeedbackRow(
                run_id=run_id, rating=body.rating, reason=body.reason, rated_at=datetime.now(UTC)
            )
            db.add(row)
        elif (row.rating, row.reason) != (body.rating, body.reason):
            row.rating = body.rating
            row.reason = body.reason
            row.rated_at = datetime.now(UTC)
        return execution.model_copy(
            update={"feedback": RunFeedback.model_validate(row, from_attributes=True)}
        )

    async def finish_execution(
        self,
        db: AsyncSession,
        running: AgentRun,
        *,
        result: AgentLearningResult | None = None,
        failure_code: str | None = None,
        assisted_version: UUID | None = None,
        verify: Callable[[AgentRun], Awaitable[None]] | None = None,
    ) -> AgentExecution:
        if (result is None) == (failure_code is None):
            raise ValueError("a learning execution requires exactly one outcome")
        row = await self._lock_run(db, running.run_id, running.revision)
        end = RunState.FAILED if result is None else RunState.SUCCEEDED
        check_run_transition(RunState(row.state), end)
        if verify is not None:
            await verify(_run_record(row))
        workflow = await db.get(AgentWorkflowRow, row.id)
        if workflow is None:
            raise AgentRecordUnavailable("Agent workflow unavailable")
        row.state = end.value
        row.revision += 1
        task = await db.get(AgentTaskRow, row.id)
        if task is not None:
            task.lease_expires_at = None
            await finish_timing(db, row.id)
        workflow.result = None if result is None else result.model_dump(mode="json")
        workflow.failure_code = failure_code
        if assisted_version is not None:
            await self._record_hint_usage(db, assisted_version)
        await clear_partial_answer(db, running.run_id)
        return AgentExecution(run=_run_record(row), result=result, failure_code=failure_code)

    async def publish_chat_output(
        self,
        running: AgentRun,
        channel: ModelOutputChannel,
        text: str,
        *,
        verify: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> None:
        column = "text" if channel == "answer" else "reasoning"
        async with self._sessions.begin() as db:
            if verify is not None:
                await verify(db)
            await self._lock_run(db, running.run_id, running.revision)
            preview = insert(AgentPartialAnswerRow).values(run_id=running.run_id, **{column: text})
            await db.execute(
                preview.on_conflict_do_update(
                    index_elements=[AgentPartialAnswerRow.run_id],
                    set_={column: text},
                )
            )

    async def _record_hint_usage(self, db: AsyncSession, version_id: UUID) -> None:
        await db.execute(
            insert(HintUsage)
            .values(owner_id=self._owner_id, version_id=version_id)
            .on_conflict_do_nothing(index_elements=[HintUsage.owner_id, HintUsage.version_id])
        )

    async def _lock_run(self, db: AsyncSession, run_id: UUID, revision: int) -> AgentRunRow:
        row = await db.scalar(
            self._owned_runs().where(AgentRunRow.id == run_id).with_for_update(of=AgentRunRow)
        )
        if row is None:
            raise AgentRecordUnavailable("Agent record unavailable")
        if row.revision != revision:
            raise AgentRecordConflict("Agent run revision changed")
        if row.state == RunState.RUNNING:
            task = await db.get(AgentTaskRow, row.id)
            if task is not None:
                now = await db.scalar(select(func.clock_timestamp()))
                if task.lease_expires_at is None or task.lease_expires_at <= now:
                    raise AgentRecordConflict("Agent learning lease expired")

        return row

    async def transition(self, run_id: UUID, revision: int, following: RunState) -> AgentRun:
        async with self._sessions.begin() as db:
            row = await self._lock_run(db, run_id, revision)
            check_run_transition(RunState(row.state), following)
            if await db.get(AgentTaskRow, row.id) is not None:
                raise AgentRecordConflict("queued learning runs use the task service")
            row.state = following.value
            row.revision += 1
            return _run_record(row)

    async def get_step(self, run_id: UUID, sequence: int) -> AgentStep:
        async with self._sessions() as db:
            row = await db.scalar(
                select(AgentStepRow)
                .join(AgentRunRow, AgentRunRow.id == AgentStepRow.run_id)
                .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
                .where(
                    AgentSessionRow.owner_id == self._owner_id,
                    AgentStepRow.run_id == run_id,
                    AgentStepRow.sequence == sequence,
                )
            )
            if row is None:
                raise AgentRecordUnavailable("Agent record unavailable")
            return _step_record(row)

    async def last_step_sequence(self, running: AgentRun) -> int:
        async with self._sessions.begin() as db:
            await self._lock_run(db, running.run_id, running.revision)
            return await db.scalar(
                select(func.coalesce(func.max(AgentStepRow.sequence), 0)).where(
                    AgentStepRow.run_id == running.run_id
                )
            )

    async def checkpoint(
        self, running: AgentRun, ordinal: int, invocation: str, arguments_digest: str
    ) -> dict | None:
        async with self._sessions.begin() as db:
            await self._lock_run(db, running.run_id, running.revision)
            found = (
                await db.execute(
                    select(
                        AgentCheckpointRow.output,
                        AgentStepRow.invocation_version,
                        AgentStepRow.arguments_digest,
                    )
                    .join(AgentStepRow, AgentStepRow.id == AgentCheckpointRow.step_id)
                    .where(
                        AgentCheckpointRow.run_id == running.run_id,
                        AgentCheckpointRow.ordinal == ordinal,
                    )
                )
            ).one_or_none()
            if found is None:
                return None
            output, saved_invocation, saved_arguments = found
            if saved_invocation != invocation or saved_arguments != arguments_digest:
                raise AgentRecordConflict("Agent checkpoint describes different tool input")
            return output

    async def begin_model_step(
        self,
        db: AsyncSession,
        running: AgentRun,
        invocation: str,
        arguments_digest: str,
        limits: AgentModelLimits | None,
        verify: Callable[[AsyncSession], Awaitable[None]],
        *,
        request_seconds: int = MODEL_REQUEST_SECONDS,
        previous_invocations: tuple[str, ...] = (),
        discarded_invocation: str | None = None,
    ) -> tuple[AgentStep | None, bool, dict | None]:
        """Resolve an existing model step before admitting any new request."""
        run = await self._lock_run(db, running.run_id, running.revision)
        if run.state != RunState.RUNNING:
            raise AgentRecordConflict("only a running Agent run can call a model")
        await verify(db)
        existing = (
            await db.execute(
                select(AgentStepRow, AgentCheckpointRow.output)
                .outerjoin(AgentCheckpointRow, AgentCheckpointRow.step_id == AgentStepRow.id)
                .where(AgentStepRow.run_id == run.id, AgentStepRow.kind == "model")
            )
        ).one_or_none()
        if existing is not None:
            step, output = existing
            if step.invocation_version == discarded_invocation:
                return _step_record(step), False, None
            if (
                step.invocation_version not in (invocation, *previous_invocations)
                or step.arguments_digest != arguments_digest
            ):
                raise AgentRecordConflict("model step input changed")
            return _step_record(step), False, output
        if limits is None:
            return None, False, None
        await reserve_model_attempt(db, run, self._owner_id, limits, request_seconds)
        previous = await db.scalar(
            select(func.coalesce(func.max(AgentStepRow.sequence), 0)).where(
                AgentStepRow.run_id == run.id
            )
        )
        step = AgentStep(
            step_id=uuid4(),
            run_id=run.id,
            sequence=previous + 1,
            kind="model",
            invocation_version=invocation,
            arguments_digest=arguments_digest,
            state="running",
        )
        db.add(
            AgentStepRow(id=step.step_id, **step.model_dump(exclude={"schema_version", "step_id"}))
        )
        return step, True, None

    async def take_source_excerpt(self, db: AsyncSession, running: AgentRun) -> bytes | None:
        return await db.scalar(
            select(func.kf_take_learning_source(running.run_id, running.revision))
        )

    async def record_step(
        self,
        step: AgentStep,
        run_revision: int,
        *,
        checkpoint: tuple[int, dict] | None = None,
        model_usage: ModelTokenUsage | None = None,
        verify: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> AgentStep:
        if checkpoint is not None and step.state != "succeeded":
            raise ValueError("only a successful Agent step has a checkpoint")
        step = AgentStep.model_validate(step.model_dump())
        async with self._sessions.begin() as db:
            if verify is not None:
                await verify(db)
            run = await self._lock_run(db, step.run_id, run_revision)
            row = await db.scalar(
                select(AgentStepRow).where(
                    AgentStepRow.run_id == run.id, AgentStepRow.sequence == step.sequence
                )
            )
            if row is None:
                if run.state != RunState.RUNNING or step.state != "running":
                    raise AgentRecordConflict("only a running Agent run can begin a step")
                inserted = await db.scalar(
                    insert(AgentStepRow)
                    .values(
                        id=step.step_id,
                        **step.model_dump(exclude={"schema_version", "step_id"}),
                    )
                    .on_conflict_do_nothing()
                    .returning(AgentStepRow.id)
                )
                if inserted is None:
                    raise AgentRecordConflict("Agent step identity already in use")
                return step
            saved = _step_record(row)
            if saved == step:
                return saved
            mutable = {"state", "result_digest", "error_code"}
            if (
                saved.model_dump(exclude=mutable) != step.model_dump(exclude=mutable)
                or saved.state != "running"
                or step.state == "running"
                or not (
                    run.state == RunState.RUNNING
                    or (run.state == RunState.CANCEL_REQUESTED and step.state == "cancelled")
                )
            ):
                raise AgentRecordConflict("Agent step cannot accept this outcome")
            row.state = step.state
            row.result_digest = step.result_digest
            row.error_code = step.error_code
            if step.kind == "model":
                await finish_model_attempt(db, step.run_id, model_usage)
            if checkpoint is not None:
                ordinal, output = checkpoint
                db.add(
                    AgentCheckpointRow(
                        run_id=step.run_id, ordinal=ordinal, step_id=step.step_id, output=output
                    )
                )
            return step
