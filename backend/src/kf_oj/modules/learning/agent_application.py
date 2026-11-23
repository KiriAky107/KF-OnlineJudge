"""Authenticated entry for a learning run and execution of a claimed workflow."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_api import AgentHistoryPage, AgentRunTrace
from kf_oj.contracts.agent_learning import (
    AgentExecution,
    AgentLearningInput,
    FocusedTrainingResult,
    GuidedHelpResult,
    PeriodicReviewResult,
    ReviewEvidenceFocus,
    StoredAgentLearningInput,
    SubmissionReviewParameters,
    SubmissionReviewResult,
    TrainingSetDraft,
    UpdateRunFeedback,
)
from kf_oj.contracts.agent_runtime import AgentPurpose, AgentRun, AgentSession, AgentStep, RunState
from kf_oj.contracts.learning import TrainingDetail
from kf_oj.contracts.learning_model import (
    GuidedHelpModelFacts,
    ModelHint,
    ModelHintInput,
    ModelHintUnavailable,
    PlanModelFacts,
    ReviewModelFacts,
    SourceExcerptModelContext,
    SourceExcerptModelFacts,
)
from kf_oj.modules.identity.public import (
    AcceptedSession,
    ActiveSessionPort,
    AuthenticationFailed,
    Principal,
)
from kf_oj.modules.learning.agent_model_budget import DEFAULT_MODEL_LIMITS, AgentModelLimits
from kf_oj.modules.learning.agent_queue import AgentQueue
from kf_oj.modules.learning.agent_store import PostgresAgentStorage
from kf_oj.modules.learning.public import (
    AGENT_TOOL_SCOPES,
    SOURCE_EVIDENCE,
    TRAINING_CANDIDATES,
    LearningUnavailable,
    scoped_agent_tools,
)
from kf_oj.modules.learning.service import LearningService
from kf_oj.modules.skills.public import (
    BuiltinSkills,
    SkillBindings,
    SkillBindingUnavailable,
    SkillVersionUnavailable,
    load_builtin_skills,
)
from kf_oj.modules.submissions.public import SubmissionEvidencePort, SubmissionUnavailable
from kf_oj.runtime.model import (
    LearningHintModel,
    ModelBudgetExceeded,
    guided_help_model_facts,
    plan_model_facts,
    review_focus_targets,
    review_model_facts,
)
from kf_oj.runtime.storage import AgentRecordConflict, AgentRecordUnavailable
from kf_oj.runtime.tools import (
    ToolArgumentsInvalid,
    ToolBudgetExceeded,
    ToolContext,
    ToolDenied,
    ToolKey,
    ToolResultInvalid,
    ToolTimedOut,
)
from kf_oj.runtime.workflows import LearningEvidenceUnavailable, LearningWorkflow

_FAILURE_CODES = {
    TimeoutError: "tool_timeout",
    ToolResultInvalid: "tool_result_invalid",
    ToolArgumentsInvalid: "tool_arguments_invalid",
    ToolBudgetExceeded: "tool_budget_exhausted",
    ToolDenied: "tool_unavailable",
    LearningEvidenceUnavailable: "evidence_unavailable",
    AuthenticationFailed: "authorization_unavailable",
    SkillBindingUnavailable: "authorization_unavailable",
    SkillVersionUnavailable: "authorization_unavailable",
}
_EXPECTED_FAILURES = tuple(_FAILURE_CODES)


def _failure_code(error: Exception) -> str:
    return next(code for kind, code in _FAILURE_CODES.items() if isinstance(error, kind))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AgentLearning:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        identity: ActiveSessionPort,
        catalog: BuiltinSkills | None = None,
        *,
        source_reader: SubmissionEvidencePort | None = None,
        model: LearningHintModel | None = None,
        model_limits: AgentModelLimits = DEFAULT_MODEL_LIMITS,
        claim: AgentRun | None = None,
    ) -> None:
        self._sessions = sessions
        self._identity = identity
        self._source_reader = source_reader
        self._model = model
        self._model_limits = model_limits
        self.catalog = catalog if catalog is not None else load_builtin_skills()
        self.skills = SkillBindings(
            sessions, identity, self.catalog, AGENT_TOOL_SCOPES, claim=claim
        )

    def _store(self, principal: Principal) -> PostgresAgentStorage:
        return PostgresAgentStorage(self._sessions, principal.account_id)

    async def _authenticate(self, principal: Principal) -> None:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)

    async def create_session(self, principal: Principal, purpose: AgentPurpose) -> AgentSession:
        await self._authenticate(principal)
        return await self._store(principal).create_session(purpose)

    @staticmethod
    async def _training_source(
        storage: PostgresAgentStorage,
        session_id: UUID,
        skill_version_id: UUID,
        draft: TrainingSetDraft | None,
    ) -> FocusedTrainingResult | None:
        if draft is None or draft.revision is None:
            return None
        revision = draft.revision
        source = await storage.execution_result(revision.run_id)
        if (
            source.run.session_id != session_id
            or source.run.skill_version_id != skill_version_id
            or source.run.state != RunState.SUCCEEDED
            or not isinstance(source.result, FocusedTrainingResult)
            or source.result.training_set is None
        ):
            raise AgentRecordUnavailable("training set draft unavailable")
        ignored = {"title", "revision"}
        if draft.model_dump(exclude=ignored) != source.result.training_set.model_dump(
            exclude=ignored
        ):
            raise ValueError("training set revisions must keep the original selection conditions")
        if revision.replace_position is not None and not any(
            item.position == revision.replace_position for item in source.result.items
        ):
            raise ValueError("training set exercise position is unavailable")
        return source.result

    async def prepare(
        self,
        principal: Principal,
        session_id: UUID,
        request_id: UUID,
        skill_version_id: UUID,
        body: AgentLearningInput,
    ) -> AgentExecution:
        await self._authenticate(principal)
        storage = self._store(principal)
        session = await storage.get_session(session_id)
        if body.training_set is not None and session.purpose != AgentPurpose.FOCUSED_TRAINING:
            raise ValueError("training set drafts require focused training")
        code_purposes = (AgentPurpose.GUIDED_HELP, AgentPurpose.SUBMISSION_REVIEW)
        if body.include_source_evidence and session.purpose not in code_purposes:
            raise ValueError("source evidence is unavailable for this learning purpose")
        if body.submission_id is not None and session.purpose not in code_purposes:
            raise ValueError("submission input requires a supported learning purpose")
        if body.model_topic and (
            session.purpose != AgentPurpose.GUIDED_HELP
            or (body.submission_id is not None and not body.include_model_verdict)
            or body.include_source_evidence
        ):
            raise ValueError("concept hints require guided help without source evidence")
        if body.include_model_verdict and (body.model_topic is None or body.submission_id is None):
            raise ValueError("model verdict consent requires a topic and submission")
        if body.include_model_review and session.purpose != AgentPurpose.SUBMISSION_REVIEW:
            raise ValueError("model review is only available for submission reviews")
        if body.include_model_evidence_focus and not body.include_model_review:
            raise ValueError("choosing review evidence requires model review consent")
        if body.include_model_code_context and not (
            body.include_source_evidence and body.include_model_review
        ):
            raise ValueError(
                "model code counts require both model review and personal code evidence"
            )
        if (
            body.include_model_control_flow or body.include_model_comparison_cases
        ) and not body.include_model_code_context:
            raise ValueError("additional model counts require personal code-count authorization")
        if body.include_model_plan_explanation and session.purpose not in (
            AgentPurpose.FOCUSED_TRAINING,
            AgentPurpose.PERIODIC_REVIEW,
        ):
            raise ValueError("model plan explanations require a training purpose")
        selection = body.model_source_excerpt
        if selection is not None and not (
            body.submission_id is not None
            and body.include_source_evidence
            and body.include_model_review
            and not (body.include_model_code_context or body.include_model_evidence_focus)
        ):
            raise ValueError("source analysis requires an explicitly selected submission excerpt")
        # Keep old request digests for fields absent before the model and source options existed.
        scope = set()
        if not body.include_source_evidence:
            scope.add("include_source_evidence")
        if body.model_topic is None:
            scope.add("model_topic")
        if not body.include_model_review:
            scope.add("include_model_review")
        if not body.include_model_code_context:
            scope.add("include_model_code_context")
        input_digest = _digest(body.model_dump_json(exclude=scope).encode())

        def same_request(run: AgentRun) -> bool:
            return run.skill_version_id == skill_version_id and run.input_digest == input_digest

        existing = await storage.find_request(session_id, request_id)
        if existing is not None:
            if not same_request(existing):
                raise AgentRecordConflict("Agent request already has different inputs")
            return await storage.execution(existing.run_id)

        await self._training_source(storage, session_id, skill_version_id, body.training_set)
        configuration = await self.skills.freeze(
            principal,
            skill_version_id,
            purpose=session.purpose,
            required_tool=TRAINING_CANDIDATES if body.training_set is not None else None,
        )
        now = datetime.now(UTC)
        snapshot = StoredAgentLearningInput(
            **body.model_dump(),
            evidence_as_of=now,
            review_anchor=body.submission_id
            if session.purpose == AgentPurpose.SUBMISSION_REVIEW
            else None,
        )
        source_excerpt = None
        if body.include_source_evidence:
            if self._source_reader is None:
                raise LearningUnavailable("source evidence unavailable")
            review = configuration.parameters
            review = review if isinstance(review, SubmissionReviewParameters) else None
            async with self._sessions.begin() as db:
                await self._identity.require_active_session(db, principal)
                grant = await self.skills.for_current_skill(
                    db, principal, skill_version_id, configuration.digest
                )
                if grant is None or SOURCE_EVIDENCE not in grant.tools:
                    raise SkillBindingUnavailable("source evidence unavailable")
                try:
                    captured = await self._source_reader.learning_code_evidence(
                        db,
                        principal,
                        since=now - timedelta(days=review.days if review else 90),
                        as_of=now,
                        problem_ids=review.problem_ids if review else (),
                        submission_id=body.submission_id,
                        comparison=session.purpose == AgentPurpose.SUBMISSION_REVIEW,
                        collect_control_flow=body.include_model_control_flow,
                    )
                    if selection is not None:
                        if captured is None or not captured.source_available:
                            raise SubmissionUnavailable("submission source unavailable")
                        excerpt = await self._source_reader.owned_source_excerpt(
                            principal,
                            captured.source.submission_id,
                            selection.start_line,
                            selection.end_line,
                        )
                        if excerpt.source != captured.source:
                            raise SubmissionUnavailable("submission source changed")
                        if not excerpt.text.strip():
                            raise ValueError("source excerpt is empty")
                        source_excerpt = excerpt.text.encode("utf-8")
                except SubmissionUnavailable:
                    raise AgentRecordUnavailable("submission evidence unavailable") from None
            snapshot = snapshot.model_copy(update={"source_evidence": captured})

        try:
            run = await storage.enqueue(
                session_id,
                request_id=request_id,
                skill_version_id=skill_version_id,
                configuration_digest=configuration.digest,
                input_digest=input_digest,
                workflow_input=snapshot,
                receipt=AcceptedSession.from_principal(principal),
                source_excerpt=source_excerpt,
            )
        except AgentRecordConflict:
            existing = await storage.find_request(session_id, request_id)
            if existing is None or not same_request(existing):
                raise
            run = existing
        return await storage.execution(run.run_id)

    async def history(self, principal: Principal) -> AgentHistoryPage:
        await self._authenticate(principal)
        return await self._store(principal).history()

    async def trace(self, principal: Principal, run_id: UUID) -> AgentRunTrace:
        await self._authenticate(principal)
        return await self._store(principal).trace(run_id)

    async def execution(self, principal: Principal, run_id: UUID) -> AgentExecution:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            execution = await self._store(principal).execution_in(db, run_id)
            if (
                isinstance(execution.result, FocusedTrainingResult)
                and execution.result.training_set
            ):
                saved = await LearningService.saved_agent_plan_id(db, principal, run_id)
                return execution.model_copy(update={"training_id": saved})
            return execution

    async def cancel(self, principal: Principal, run_id: UUID) -> AgentExecution:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            await AgentQueue(self._sessions).cancel(db, principal.account_id, run_id)
        return await self._store(principal).execution(run_id)

    async def save_feedback(
        self, principal: Principal, run_id: UUID, body: UpdateRunFeedback
    ) -> AgentExecution:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            return await self._store(principal).save_feedback(db, run_id, body)

    async def save_plan(self, principal: Principal, run_id: UUID) -> TrainingDetail:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            execution = await self._store(principal).execution_in(db, run_id, lock=True)
            if execution.run.state != RunState.SUCCEEDED or not isinstance(
                execution.result, (FocusedTrainingResult, PeriodicReviewResult)
            ):
                raise LearningUnavailable("learning run has no available training draft")

            service = LearningService(self._sessions)
            previous = await service.saved_agent_plan(db, principal, run_id)
            if previous is not None:
                return previous
            result = execution.result
            config = await self.skills.require_execution_configuration(
                db,
                principal,
                execution.run.skill_version_id,
                execution.run.configuration_digest,
                required_tool=TRAINING_CANDIDATES
                if isinstance(result, FocusedTrainingResult) and result.training_set is not None
                else None,
            )
            return await service.save_agent_plan(db, principal, run_id, result, config.parameters)

    async def _add_model_hint[
        WithHint: (
            GuidedHelpResult,
            SubmissionReviewResult,
            FocusedTrainingResult,
            PeriodicReviewResult,
        )
    ](
        self,
        principal: Principal,
        storage: PostgresAgentStorage,
        running: AgentRun,
        result: WithHint,
        content: ModelHintInput | SourceExcerptModelContext,
        ordinal: int,
    ) -> WithHint:
        source_context = content if isinstance(content, SourceExcerptModelContext) else None
        model = self._model
        if source_context is not None and not getattr(model, "accepts_source_excerpt", False):
            model = None

        async def verify(db: AsyncSession) -> None:
            await self.skills.require_execution_configuration(
                db, principal, running.skill_version_id, running.configuration_digest
            )

        previous_invocations = ()
        if source_context is not None:
            invocation = "responses-source-excerpt-v1"
            raw = source_context.model_dump_json().encode()
        elif isinstance(content, ReviewModelFacts):
            if content.code is not None and content.code.comparison_cases is not None:
                invocation = "responses-review-comparison-cases-v1"
            elif content.code is not None and content.code.control_flow is not None:
                invocation = (
                    "responses-review-control-flow-focus-v1"
                    if content.focus_options
                    else "responses-review-control-flow-v1"
                )
            elif content.focus_options:
                invocation = "responses-review-evidence-focus-v1"
            elif content.code is not None:
                invocation = "responses-review-code-counts-v1"
            else:
                invocation = "responses-review-v1"
            prefix = invocation.removesuffix("-v1")
            previous_invocations = (invocation, prefix + "-v2")
            invocation = prefix + "-v3"
            raw = content.model_dump_json().encode()
        elif isinstance(content, GuidedHelpModelFacts):
            invocation = "responses-concept-verdict-v1"
            raw = content.model_dump_json().encode()
        elif isinstance(content, PlanModelFacts):
            invocation = "responses-plan-explanation-v1"
            raw = content.model_dump_json(exclude_none=True).encode()
        else:
            invocation = "responses-concept-v1"
            raw = content.encode()
        try:
            async with self._sessions.begin() as db:
                await self._identity.require_active_session(db, principal)
                step, dispatch, checkpoint = await storage.begin_model_step(
                    db,
                    running,
                    invocation,
                    _digest(raw),
                    self._model_limits if model is not None else None,
                    verify,
                    previous_invocations=previous_invocations,
                )
                if dispatch and source_context is not None:
                    excerpt = await storage.take_source_excerpt(db, running)
                    if excerpt is None:
                        raise ModelHintUnavailable("source excerpt unavailable")
                    content = SourceExcerptModelFacts(
                        **source_context.model_dump(), text=excerpt.decode("utf-8")
                    )
        except ModelBudgetExceeded:
            return result.model_copy(update={"model_status": "budget_exhausted"})
        except ModelHintUnavailable:
            return result.model_copy(update={"model_status": "unavailable"})
        if step is None:
            unavailable = "source_model_unavailable" if source_context else "unavailable"
            return result.model_copy(update={"model_status": unavailable})
        if not dispatch:
            if checkpoint is not None:
                return result.model_copy(
                    update={
                        "model_hint": ModelHint.model_validate(checkpoint),
                        "model_status": "generated",
                    }
                )
            status = "unavailable" if step.state == "failed" else "uncertain"
            return result.model_copy(update={"model_status": status})
        assert model is not None
        try:
            hint = await model.hint(content)
            if source_context is not None and (
                hint.source_line is None
                or not source_context.start_line <= hint.source_line <= source_context.end_line
            ):
                raise ModelHintUnavailable(
                    "invalid source analysis citation",
                    code="model_invalid_response",
                    usage=hint.usage,
                )
        except ModelHintUnavailable as error:
            await storage.record_step(
                step.model_copy(update={"state": "failed", "error_code": error.code}),
                running.revision,
                model_usage=error.usage,
            )
            return result.model_copy(update={"model_status": "unavailable"})
        await storage.record_step(
            step.model_copy(
                update={
                    "state": "succeeded",
                    "result_digest": _digest(hint.model_dump_json().encode()),
                }
            ),
            running.revision,
            checkpoint=(ordinal, hint.model_dump(mode="json")),
            model_usage=hint.usage,
        )
        return result.model_copy(update={"model_hint": hint, "model_status": "generated"})

    async def execute_claimed(
        self, principal: Principal, run_id: UUID, revision: int
    ) -> AgentExecution:
        storage = self._store(principal)
        running = await storage.get_run(run_id)
        if running.state != RunState.RUNNING or running.revision != revision:
            raise AgentRecordConflict("Agent run is not held by this execution")
        context = ToolContext(run_id, revision)
        sequence = await storage.last_step_sequence(running)
        ordinal = 0

        async def invoke(key: ToolKey, arguments: BaseModel) -> BaseModel:
            nonlocal ordinal, sequence
            ordinal += 1
            raw = arguments.model_dump_json().encode()
            invocation = f"{key.name}-v{key.version}"
            arguments_digest = _digest(raw)
            checkpoint = await storage.checkpoint(running, ordinal, invocation, arguments_digest)
            if checkpoint is not None:
                return await tools.restore(context, key, checkpoint)
            sequence += 1
            step = AgentStep(
                step_id=uuid4(),
                run_id=run_id,
                sequence=sequence,
                kind="tool",
                invocation_version=invocation,
                arguments_digest=arguments_digest,
                state="running",
            )
            await storage.record_step(step, revision)
            try:
                result = await tools.invoke(context, key, raw)
            except _EXPECTED_FAILURES as error:
                await storage.record_step(
                    step.model_copy(update={"state": "failed", "error_code": _failure_code(error)}),
                    revision,
                )
                raise
            await storage.record_step(
                step.model_copy(
                    update={
                        "state": "succeeded",
                        "result_digest": _digest(result.model_dump_json().encode()),
                    }
                ),
                revision,
                checkpoint=(ordinal, result.model_dump(mode="json")),
            )
            return result

        try:
            await self._authenticate(principal)
            body = await storage.workflow_input(running)
            tools = scoped_agent_tools(
                self._sessions,
                principal,
                self._identity,
                self.skills,
                source_evidence=body.source_evidence,
                as_of=body.evidence_as_of or running.created_at,
            )
            required_tool = TRAINING_CANDIDATES if body.training_set is not None else None
            config = await self.skills.configuration(
                principal,
                running.skill_version_id,
                running.configuration_digest,
                required_tool=required_tool,
            )
            workflow = LearningWorkflow(invoke)
            try:
                training_source = await self._training_source(
                    storage, running.session_id, running.skill_version_id, body.training_set
                )
            except (AgentRecordUnavailable, ValueError) as error:
                raise LearningEvidenceUnavailable("training set draft unavailable") from error
            result = await workflow.run(
                config.parameters,
                body,
                review_anchor=body.review_anchor,
                training_source=training_source,
            )
            if body.model_topic is not None and isinstance(result, GuidedHelpResult):
                content = (
                    guided_help_model_facts(body.model_topic, result.evidence)
                    if body.include_model_verdict
                    else body.model_topic
                )
                if content is None:
                    result = result.model_copy(update={"model_status": "insufficient_evidence"})
                else:
                    result = await self._add_model_hint(
                        principal, storage, running, result, content, ordinal + 1
                    )
                    if result.model_status == "generated" and body.include_model_verdict:
                        result = result.model_copy(update={"model_hint_basis": "topic_and_verdict"})
            if body.model_source_excerpt is not None and isinstance(result, SubmissionReviewResult):
                result = result.model_copy(update={"model_review_basis": "source_excerpt"})
                if result.code is None or not result.code.source_available:
                    result = result.model_copy(update={"model_status": "insufficient_evidence"})
                else:
                    result = await self._add_model_hint(
                        principal,
                        storage,
                        running,
                        result,
                        SourceExcerptModelContext(
                            **body.model_source_excerpt.model_dump(),
                            language=result.code.source.language,
                        ),
                        ordinal + 1,
                    )
            elif (
                body.include_model_review
                and isinstance(result, SubmissionReviewResult)
                and isinstance(config.parameters, SubmissionReviewParameters)
            ):
                targets = review_focus_targets(result) if body.include_model_evidence_focus else {}
                facts = review_model_facts(
                    result,
                    include_code_counts=body.include_model_code_context,
                    include_control_flow=body.include_model_control_flow,
                    include_comparison_cases=body.include_model_comparison_cases,
                    focus_options=tuple(targets),
                )
                if facts is None:
                    result = result.model_copy(update={"model_status": "insufficient_evidence"})
                else:
                    basis = "evaluations"
                    if facts.code is not None:
                        basis = (
                            "evaluations_and_control_flow"
                            if facts.code.control_flow is not None
                            else "evaluations_and_code_counts"
                        )
                        if facts.code.comparison_cases is not None:
                            basis = "evaluations_and_comparison_cases"
                    result = result.model_copy(update={"model_review_basis": basis})
                    result = await self._add_model_hint(
                        principal, storage, running, result, facts, ordinal + 1
                    )
                    choice = result.model_hint.review_focus if result.model_hint else None
                    if choice is not None and (target := targets.get(choice)) is not None:
                        ordinal += 1
                        sequence = await storage.last_step_sequence(running)
                        try:
                            verified = await workflow.verify_review_evidence(
                                config.parameters, target
                            )
                        except (ToolBudgetExceeded, ToolTimedOut):
                            verified = None
                        if verified is not None:
                            result = result.model_copy(
                                update={
                                    "model_focus": ReviewEvidenceFocus(
                                        category=choice, evidence=verified
                                    )
                                }
                            )
            if body.include_model_plan_explanation and isinstance(
                result, (FocusedTrainingResult, PeriodicReviewResult)
            ):
                if result.items:
                    result = await self._add_model_hint(
                        principal, storage, running, result, plan_model_facts(result), ordinal + 1
                    )
                else:
                    result = result.model_copy(update={"model_status": "insufficient_evidence"})
            async with self._sessions.begin() as db:
                await self._identity.require_active_session(db, principal)

                async def verify(actual: AgentRun) -> None:
                    await self.skills.require_execution_configuration(
                        db,
                        principal,
                        actual.skill_version_id,
                        actual.configuration_digest,
                        required_tool=required_tool,
                    )

                result = result.model_copy(update={"evidence_as_of": body.evidence_as_of})
                return await storage.finish_execution(db, running, result=result, verify=verify)
        except _EXPECTED_FAILURES as error:
            async with self._sessions.begin() as db:
                if isinstance(error, ToolTimedOut) and error.retryable:
                    deferred = await AgentQueue(self._sessions).defer(db, running)
                    if deferred is not None:
                        return AgentExecution(run=deferred)
                return await storage.finish_execution(
                    db, running, failure_code=_failure_code(error)
                )
