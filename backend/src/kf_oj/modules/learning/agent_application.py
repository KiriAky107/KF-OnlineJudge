"""Authenticated entry for a learning run and execution of a claimed workflow."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_api import (
    AgentConversationPage,
    AgentHistoryPage,
    AgentRunTrace,
    ConversationPage,
    MistakePractice,
    MistakePracticeRequest,
)
from kf_oj.contracts.agent_learning import (
    AgentExecution,
    AgentLearningInput,
    FocusedTrainingParameters,
    FocusedTrainingResult,
    GuidedHelpParameters,
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
from kf_oj.contracts.diagnostic_batch import RegisteredBatchOffer
from kf_oj.contracts.learning import TrainingDetail
from kf_oj.contracts.learning_model import (
    CHAT_HISTORY_LIMIT,
    CHAT_MODEL_REQUEST_SECONDS,
    CHAT_STATEMENT_LIMIT,
    MODEL_REQUEST_SECONDS,
    PROGRESSIVE_HINT_LEVELS,
    REVIEW_MODEL_REQUEST_SECONDS,
    GuidedChatModelFacts,
    GuidedDiagnosticChoice,
    GuidedHelpModelFacts,
    GuidedHintChoice,
    LearningChatRequest,
    LearningChatTurn,
    LearningHintCeiling,
    ModelHint,
    ModelHintInput,
    ModelHintUnavailable,
    ModelOutputChannel,
    PlanModelFacts,
    RecentPracticeFact,
    ReviewBatchOutcome,
    ReviewEvaluationSummary,
    ReviewModelFacts,
    ReviewResourceName,
    SourceExcerptModelContext,
    SourceExcerptModelFacts,
)
from kf_oj.modules.contests.public import AgentUnavailableDuringContest, require_agent_access
from kf_oj.modules.identity.public import (
    AcceptedSession,
    ActiveSessionPort,
    AuthenticationFailed,
    Principal,
)
from kf_oj.modules.learning.agent_model_budget import DEFAULT_MODEL_LIMITS, AgentModelLimits
from kf_oj.modules.learning.agent_queue import AgentQueue
from kf_oj.modules.learning.agent_store import PostgresAgentStorage
from kf_oj.modules.learning.agent_tool_policy import reserve_review_evaluation_read
from kf_oj.modules.learning.diagnostic_queue import DiagnosticQueue
from kf_oj.modules.learning.guided_delivery import (
    LOCAL_SOURCE_REQUEST,
    hint_choices,
    local_diagnostic,
)
from kf_oj.modules.learning.hint_policy import DEFAULT_HINT_POLICY, HintCeilingDenied, HintPolicy
from kf_oj.modules.learning.mistakes import PersonalMistakes
from kf_oj.modules.learning.public import (
    AGENT_TOOL_SCOPES,
    SOURCE_EVIDENCE,
    SUBMISSION_HISTORY,
    TRAINING_CANDIDATES,
    LearningUnavailable,
    scoped_agent_tools,
)
from kf_oj.modules.learning.service import LearningService
from kf_oj.modules.problems import public as problems
from kf_oj.modules.skills.public import (
    BuiltinSkills,
    SkillBindings,
    SkillBindingUnavailable,
    SkillConfiguration,
    SkillVersionUnavailable,
    load_builtin_skills,
)
from kf_oj.modules.submissions.public import (
    SubmissionEvidencePort,
    SubmissionUnavailable,
    accepted_versions,
    owned_guided_submission_details,
    owned_review_evaluation_category,
    recent_practice_versions,
)
from kf_oj.runtime.model import (
    ChatResourceModel,
    LearningHintModel,
    ModelBudgetExceeded,
    ReviewResourceModel,
    StreamingLearningHintModel,
    StreamingReviewModel,
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
    AgentUnavailableDuringContest: "contest_in_progress",
    SkillBindingUnavailable: "authorization_unavailable",
    SkillVersionUnavailable: "authorization_unavailable",
    HintCeilingDenied: "hint_level_unavailable",
}
_EXPECTED_FAILURES = tuple(_FAILURE_CODES)
_BASIC_CHAT_DIGEST = hashlib.sha256(b"kf-oj:basic-learning-chat:v1").hexdigest()


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
        hint_policy: HintPolicy = DEFAULT_HINT_POLICY,
        claim: AgentRun | None = None,
        diagnostics: DiagnosticQueue | None = None,
        review_batch_offers: tuple[RegisteredBatchOffer, ...] = (),
    ) -> None:
        self._sessions = sessions
        self._identity = identity
        self._source_reader = source_reader
        self._model = model
        self._model_limits = model_limits
        self._hint_policy = hint_policy
        self.diagnostics = diagnostics
        self._review_batch_offers = review_batch_offers
        self.mistakes = PersonalMistakes(sessions, identity)
        self.catalog = catalog if catalog is not None else load_builtin_skills()
        self.skills = SkillBindings(
            sessions, identity, self.catalog, AGENT_TOOL_SCOPES, claim=claim
        )

    def _store(self, principal: Principal) -> PostgresAgentStorage:
        return PostgresAgentStorage(self._sessions, principal.account_id)

    async def check_contest_access(self, principal: Principal) -> None:
        async with self._sessions() as db:
            await require_agent_access(db, principal.account_id)

    async def practice_mistake(
        self, principal: Principal, request: MistakePracticeRequest
    ) -> MistakePractice:
        async def authorize_tool(db: AsyncSession) -> FocusedTrainingParameters:
            parameters = await self.skills.require_enabled_tool(
                db,
                principal,
                request.skill_version_id,
                purpose=AgentPurpose.FOCUSED_TRAINING,
                required_tool=TRAINING_CANDIDATES,
            )
            assert isinstance(parameters, FocusedTrainingParameters)
            return parameters

        return await self.mistakes.practice(principal, request, authorize_tool)

    async def _authenticate(self, principal: Principal) -> bool:
        async with self._sessions.begin() as db:
            return await self._identity.require_active_session(db, principal)

    def _require_legacy_guided_ceiling(self, body: AgentLearningInput, administrator: bool) -> None:
        if body.include_source_evidence:
            level = "L3"
        elif body.include_model_verdict or body.model_topic is None:
            level = "L2"
        else:
            level = "L1"
        maximum = self._hint_policy.role_ceiling(administrator)
        if PROGRESSIVE_HINT_LEVELS.index(level) > PROGRESSIVE_HINT_LEVELS.index(maximum):
            raise HintCeilingDenied("legacy guidance exceeds the current hint policy")

    async def hint_ceiling(
        self, principal: Principal, problem_version_id: UUID, training_id: UUID | None
    ) -> LearningHintCeiling:
        async with self._sessions.begin() as db:
            administrator = await self._identity.require_active_session(db, principal)
            maximum = await self._hint_policy.ceiling(
                db, principal.account_id, administrator, problem_version_id, training_id
            )
            return LearningHintCeiling(max_hint_level=maximum)

    async def _require_chat_ceiling(
        self,
        db: AsyncSession,
        principal: Principal,
        chat: LearningChatRequest,
        administrator: bool,
        *,
        include_source: bool,
    ) -> None:
        if chat.problem_version_id is None:
            if chat.training_id is not None or include_source or chat.include_submission_details:
                raise HintCeilingDenied("chat evidence requires a problem version")
            return
        level = chat.hint_level or "L0"
        maximum = await self._hint_policy.ceiling(
            db, principal.account_id, administrator, chat.problem_version_id, chat.training_id
        )
        if (
            PROGRESSIVE_HINT_LEVELS.index(level) > PROGRESSIVE_HINT_LEVELS.index(maximum)
            or (chat.include_submission_details and level not in {"L2", "L3"})
            or (include_source and level != "L3")
        ):
            raise HintCeilingDenied("this hint or its evidence is outside the current policy")

    async def _require_chat_evidence_grants(
        self,
        db: AsyncSession,
        principal: Principal,
        skill_version_id: UUID,
        digest: str,
        chat: LearningChatRequest,
        *,
        include_source: bool,
    ) -> None:
        if not (chat.include_submission_details or include_source):
            return
        grant = await self.skills.for_current_skill(db, principal, skill_version_id, digest)
        if (
            grant is None
            or (chat.include_submission_details and SUBMISSION_HISTORY not in grant.tools)
            or (include_source and SOURCE_EVIDENCE not in grant.tools)
        ):
            raise SkillBindingUnavailable("submission evidence unavailable")

    async def create_session(
        self,
        principal: Principal,
        purpose: AgentPurpose,
        *,
        conversation_session_id: UUID | None = None,
    ) -> AgentSession:
        await self._authenticate(principal)
        return await self._store(principal).create_session(
            purpose, conversation_session_id=conversation_session_id
        )

    async def _training_source(
        self,
        storage: PostgresAgentStorage,
        session_id: UUID,
        skill_version_id: UUID,
        draft: TrainingSetDraft | None,
    ) -> FocusedTrainingResult | None:
        if draft is None:
            return None
        revision = draft.revision
        previous = revision.run_id if revision is not None else draft.previous_run_id
        if previous is None:
            return None
        source = await storage.execution_result(previous)
        if (
            source.run.session_id != session_id
            or source.run.skill_version_id != skill_version_id
            or source.run.state != RunState.SUCCEEDED
            or not isinstance(source.result, FocusedTrainingResult)
            or source.result.training_set is None
        ):
            raise AgentRecordUnavailable("training set draft unavailable")
        if revision is None:
            return None
        if source.result.training_set.search is not None:
            raise ValueError("problem search is not a training draft")
        ignored = {"title", "revision", "message", "previous_run_id"}
        if draft.model_dump(exclude=ignored) != source.result.training_set.model_dump(
            exclude=ignored
        ):
            raise ValueError("training set revisions must keep the original selection conditions")
        if revision.replace_position is not None and not any(
            item.position == revision.replace_position for item in source.result.items
        ):
            raise ValueError("training set exercise position is unavailable")
        async with self._sessions() as db:
            if not await self._training_draft_is_current(db, source.result):
                raise LearningUnavailable("training set draft contains unavailable problems")
        return source.result

    @staticmethod
    async def _training_draft_is_current(db: AsyncSession, draft: FocusedTrainingResult) -> bool:
        versions = await problems.learning_versions(
            db, {item.problem_version_id for item in draft.items}
        )
        for item in draft.items:
            version = versions.get(item.problem_version_id)
            if (
                version is None
                or version.problem_id != item.problem_id
                or not version.available
                or not version.is_current_version
            ):
                return False
        return True

    @staticmethod
    async def _recent_practice_facts(
        db: AsyncSession, principal: Principal, as_of: datetime
    ) -> tuple[RecentPracticeFact, ...]:
        recent = await recent_practice_versions(
            db, principal, since=as_of - timedelta(days=30), as_of=as_of
        )
        metadata = await problems.learning_versions(db, set(recent))
        available = {version_id for version_id, problem in metadata.items() if problem.available}
        passed = await accepted_versions(db, principal, available, as_of=as_of)
        difficulties = await problems.learning_difficulties(db, available)
        practice = []
        for version_id in recent:
            if version_id not in available:
                continue
            difficulty = difficulties.get(version_id)
            practice.append(
                RecentPracticeFact(
                    title=metadata[version_id].title,
                    difficulty=difficulty.label if difficulty else metadata[version_id].difficulty,
                    difficulty_score=difficulty.score if difficulty else None,
                    accepted=version_id in passed,
                )
            )
        return tuple(practice)

    async def _chat_context(
        self,
        principal: Principal,
        storage: PostgresAgentStorage,
        session_id: UUID,
        configuration: SkillConfiguration,
        chat: LearningChatRequest,
        *,
        include_source: bool,
    ) -> GuidedChatModelFacts:
        basic_chat = configuration.digest == _BASIC_CHAT_DIGEST
        level = (chat.hint_level or "L0") if chat.problem_version_id else None
        if level is None and (
            chat.hint_level or chat.training_id or chat.include_submission_details or include_source
        ):
            raise ValueError("progressive hints and their evidence require a problem version")
        if chat.include_submission_details and level not in {"L2", "L3"}:
            raise ValueError("submission details require at least a level L2 hint")
        if include_source and level != "L3":
            raise ValueError("source evidence requires a level L3 hint")

        previous_level = 0
        can_advance = False
        history = ()
        if chat.previous_run_id is not None:
            previous = await storage.execution(chat.previous_run_id)
            if (
                previous.run.session_id != session_id
                or (
                    previous.run.skill_version_id != configuration.version_id
                    and not basic_chat
                    and previous.run.configuration_digest != _BASIC_CHAT_DIGEST
                )
                or previous.run.state
                not in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED)
                or previous.chat is None
                or previous.chat.problem_version_id != chat.problem_version_id
                or previous.chat.training_id != chat.training_id
            ):
                raise AgentRecordUnavailable("previous chat turn unavailable")
            prior_hint = (
                previous.result.model_hint
                if previous.run.state == RunState.SUCCEEDED
                and isinstance(previous.result, GuidedHelpResult)
                else None
            )
            can_advance = prior_hint is not None and previous.chat.hint_level is not None
            previous_level = PROGRESSIVE_HINT_LEVELS.index(previous.chat.hint_level or "L0")
            same_evidence_scope = previous.run.configuration_digest in (
                configuration.digest,
                _BASIC_CHAT_DIGEST,
            )
            if same_evidence_scope and (
                level is None
                or (
                    previous.chat.hint_level is not None
                    and PROGRESSIVE_HINT_LEVELS.index(level) >= previous_level
                )
            ):
                history = previous.chat_history
                if prior_hint is not None and not previous.chat_private_turn:
                    history = (
                        *history,
                        LearningChatTurn(
                            question=previous.chat.question,
                            answer=prior_hint.text,
                        ),
                    )[-CHAT_HISTORY_LIMIT:]

        if (
            level is not None
            and PROGRESSIVE_HINT_LEVELS.index(level) > previous_level + can_advance
        ):
            raise ValueError("complete the preceding hint level before advancing")

        title = statement = None
        sample_count = None
        async with self._sessions.begin() as db:
            administrator = await self._identity.require_active_session(db, principal)
            await self._require_chat_ceiling(
                db, principal, chat, administrator, include_source=include_source
            )
            if chat.problem_version_id is not None:
                context = await problems.learning_statement(db, chat.problem_version_id)
                if context is None:
                    raise AgentRecordUnavailable("problem statement unavailable")
                title, statement, sample_count = context
                statement = statement[:CHAT_STATEMENT_LIMIT]

        return GuidedChatModelFacts(
            question=chat.question,
            hint_level=level,
            history=history,
            problem_title=title,
            problem_statement=statement,
            public_sample_count=sample_count,
        )

    async def _chat_source(
        self,
        principal: Principal,
        snapshot: StoredAgentLearningInput,
    ) -> tuple[StoredAgentLearningInput, bytes]:
        chat, submission_id, selection = (
            snapshot.chat,
            snapshot.submission_id,
            snapshot.model_source_excerpt,
        )
        assert chat is not None and submission_id is not None and selection is not None
        if self._source_reader is None:
            raise LearningUnavailable("submission evidence unavailable")

        try:
            excerpt = await self._source_reader.owned_source_excerpt(
                principal, submission_id, selection.start_line, selection.end_line
            )
            if chat.problem_version_id != excerpt.source.problem_version_id:
                raise SubmissionUnavailable("submission is for a different problem")
        except SubmissionUnavailable:
            raise AgentRecordUnavailable("submission evidence unavailable") from None
        if not excerpt.text.strip():
            raise ValueError("source excerpt is empty")
        source = SourceExcerptModelContext(
            start_line=excerpt.start_line,
            end_line=excerpt.end_line,
            language=excerpt.source.language,
        )
        return snapshot.model_copy(update={"chat_source": source}), excerpt.text.encode("utf-8")

    async def prepare(
        self,
        principal: Principal,
        session_id: UUID,
        request_id: UUID,
        skill_version_id: UUID | None,
        body: AgentLearningInput,
    ) -> AgentExecution:
        administrator = await self._authenticate(principal)
        storage = self._store(principal)
        session = await storage.get_session(session_id)
        basic_chat = skill_version_id is None
        if skill_version_id is None:
            if (
                session.purpose != AgentPurpose.GUIDED_HELP
                or body.chat is None
                or body.chat.include_submission_details
                or body.model_dump(exclude={"chat"}, exclude_defaults=True)
            ):
                raise ValueError("this request needs an explicit skill grant")
            version = max(
                self.catalog.list_versions(AgentPurpose.GUIDED_HELP),
                key=lambda entry: entry.declaration.release,
            )
            skill_version_id = version.version_id
        if session.purpose == AgentPurpose.GUIDED_HELP and body.chat is None:
            self._require_legacy_guided_ceiling(body, administrator)
        if body.periodic_review is not None and session.purpose != AgentPurpose.PERIODIC_REVIEW:
            raise ValueError("a chat review plan requires the periodic review skill")
        if body.training_set is not None and session.purpose != AgentPurpose.FOCUSED_TRAINING:
            raise ValueError("training set drafts require focused training")
        if (
            body.training_set is not None
            and body.training_set.search is not None
            and body.include_model_plan_explanation
        ):
            raise ValueError("problem search does not use the model")
        code_purposes = (AgentPurpose.GUIDED_HELP, AgentPurpose.SUBMISSION_REVIEW)
        if body.include_source_evidence and session.purpose not in code_purposes:
            raise ValueError("source evidence is unavailable for this learning purpose")
        if body.submission_id is not None and session.purpose not in code_purposes:
            raise ValueError("submission input requires a supported learning purpose")
        if body.chat and (
            session.purpose != AgentPurpose.GUIDED_HELP
            or body.model_topic is not None
            or body.include_model_verdict
            or body.include_source_evidence != (body.model_source_excerpt is not None)
            or (body.submission_id is not None)
            != (body.chat.include_submission_details or body.model_source_excerpt is not None)
        ):
            raise ValueError("chat requires explicit consent for submission evidence")
        if body.model_topic and (
            session.purpose != AgentPurpose.GUIDED_HELP
            or (body.submission_id is not None and not body.include_model_verdict)
            or body.include_source_evidence
        ):
            raise ValueError("concept hints require guided help without source evidence")
        if body.include_model_verdict and (body.model_topic is None or body.submission_id is None):
            raise ValueError("model verdict consent requires a topic and submission")
        if body.review_question is not None and (
            session.purpose != AgentPurpose.SUBMISSION_REVIEW
            or body.submission_id is None
            or not body.include_model_review
            or body.include_source_evidence != (body.model_source_excerpt is not None)
            or body.include_model_code_context
        ):
            raise ValueError("a review question requires separately scoped submission evidence")
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
            and (body.include_model_review or body.chat is not None)
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
            return (
                run.skill_version_id == skill_version_id
                and run.input_digest == input_digest
                and (run.configuration_digest == _BASIC_CHAT_DIGEST) == basic_chat
            )

        existing = await storage.find_request(session_id, request_id)
        if existing is not None:
            if not same_request(existing):
                raise AgentRecordConflict("Agent request already has different inputs")
            return await self.execution(principal, existing.run_id)

        await self._training_source(storage, session_id, skill_version_id, body.training_set)
        required_tool = None
        if body.training_set is not None or body.periodic_review is not None:
            required_tool = TRAINING_CANDIDATES
        elif body.review_question is not None:
            required_tool = SUBMISSION_HISTORY
        configuration = (
            SkillConfiguration(skill_version_id, _BASIC_CHAT_DIGEST, GuidedHelpParameters())
            if basic_chat
            else await self.skills.freeze(
                principal,
                skill_version_id,
                purpose=session.purpose,
                required_tool=required_tool,
            )
        )
        chat_context = (
            await self._chat_context(
                principal,
                storage,
                session_id,
                configuration,
                body.chat,
                include_source=selection is not None,
            )
            if body.chat is not None
            else None
        )
        now = datetime.now(UTC)
        chat = body.chat
        if chat is not None and chat_context is not None:
            chat = chat.model_copy(update={"hint_level": chat_context.hint_level})
        snapshot = StoredAgentLearningInput(
            **body.model_dump(exclude={"chat"}),
            chat=chat,
            chat_context=chat_context,
            evidence_as_of=now,
            review_anchor=body.submission_id
            if session.purpose == AgentPurpose.SUBMISSION_REVIEW
            else None,
        )
        source_excerpt = None
        if body.chat is not None and selection is not None:
            async with self._sessions.begin() as db:
                await require_agent_access(db, principal.account_id)
                administrator = await self._identity.require_active_session(db, principal)
                assert snapshot.chat is not None
                await self._require_chat_ceiling(
                    db,
                    principal,
                    snapshot.chat,
                    administrator,
                    include_source=selection is not None,
                )
                await self._require_chat_evidence_grants(
                    db,
                    principal,
                    skill_version_id,
                    configuration.digest,
                    snapshot.chat,
                    include_source=selection is not None,
                )
                snapshot, source_excerpt = await self._chat_source(principal, snapshot)
        elif body.review_question is not None and selection is not None:
            assert body.submission_id is not None
            if self._source_reader is None:
                raise LearningUnavailable("source evidence unavailable")
            async with self._sessions.begin() as db:
                await require_agent_access(db, principal.account_id)
                await self._identity.require_active_session(db, principal)
                grant = await self.skills.for_current_skill(
                    db, principal, skill_version_id, configuration.digest
                )
                if grant is None or SOURCE_EVIDENCE not in grant.tools:
                    raise SkillBindingUnavailable("source evidence unavailable")
                try:
                    excerpt = await self._source_reader.owned_source_excerpt(
                        principal, body.submission_id, selection.start_line, selection.end_line
                    )
                except SubmissionUnavailable:
                    raise AgentRecordUnavailable("submission evidence unavailable") from None
            if not excerpt.text.strip():
                raise ValueError("source excerpt is empty")
            snapshot = snapshot.model_copy(
                update={
                    "review_source": SourceExcerptModelContext(
                        start_line=excerpt.start_line,
                        end_line=excerpt.end_line,
                        language=excerpt.source.language,
                        question=body.review_question,
                    )
                }
            )
            source_excerpt = excerpt.text.encode("utf-8")
        elif body.include_source_evidence or (
            body.review_question is not None and self._source_reader is not None
        ):
            if self._source_reader is None:
                raise LearningUnavailable("source evidence unavailable")
            review = configuration.parameters
            review = review if isinstance(review, SubmissionReviewParameters) else None
            captured = None
            async with self._sessions.begin() as db:
                await require_agent_access(db, principal.account_id)
                await self._identity.require_active_session(db, principal)
                grant = await self.skills.for_current_skill(
                    db, principal, skill_version_id, configuration.digest
                )
                if grant is not None and SOURCE_EVIDENCE in grant.tools:
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
                elif body.include_source_evidence:
                    raise SkillBindingUnavailable("source evidence unavailable")
            snapshot = snapshot.model_copy(update={"source_evidence": captured})

        async def freeze_chat_evaluation(
            db: AsyncSession, workflow_input: StoredAgentLearningInput
        ) -> StoredAgentLearningInput:
            chat, facts = workflow_input.chat, workflow_input.chat_context
            submission_id, as_of = workflow_input.submission_id, workflow_input.evidence_as_of
            assert chat is not None and chat.problem_version_id is not None
            assert facts is not None and submission_id is not None and as_of is not None
            await require_agent_access(db, principal.account_id)
            administrator = await self._identity.require_active_session(db, principal)
            include_source = workflow_input.chat_source is not None
            await self._require_chat_ceiling(
                db, principal, chat, administrator, include_source=include_source
            )
            await self._require_chat_evidence_grants(
                db,
                principal,
                skill_version_id,
                configuration.digest,
                chat,
                include_source=include_source,
            )
            evidence = await owned_guided_submission_details(
                db,
                principal,
                submission_id=submission_id,
                problem_version_id=chat.problem_version_id,
                as_of=as_of,
            )
            if evidence is None:
                raise AgentRecordUnavailable("submission evidence unavailable")
            if not await reserve_review_evaluation_read(
                db, principal.account_id, chat.problem_version_id
            ):
                return workflow_input
            return workflow_input.model_copy(
                update={"chat_context": facts.model_copy(update={"submission": evidence})}
            )

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
                freeze_workflow=freeze_chat_evaluation
                if snapshot.chat is not None and snapshot.chat.include_submission_details
                else None,
            )
        except AgentRecordConflict:
            existing = await storage.find_request(session_id, request_id)
            if existing is None or not same_request(existing):
                raise
            run = existing
        return await self.execution(principal, run.run_id)

    async def history(
        self,
        principal: Principal,
        *,
        group_chat: bool = False,
        problem_version_id: UUID | None = None,
        training_drafts: bool = False,
    ) -> AgentHistoryPage:
        await self._authenticate(principal)
        return await self._store(principal).history(
            group_chat=group_chat,
            problem_version_id=problem_version_id,
            training_drafts=training_drafts,
        )

    async def recent_conversations(
        self, principal: Principal, problem_version_id: UUID | None
    ) -> AgentHistoryPage:
        await self._authenticate(principal)
        return await self._store(principal).recent_conversations(problem_version_id)

    async def trace(self, principal: Principal, run_id: UUID) -> AgentRunTrace:
        await self._authenticate(principal)
        return await self._store(principal).trace(run_id)

    async def conversation(
        self, principal: Principal, before: UUID, limit: int
    ) -> ConversationPage:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            return await self._store(principal).conversation(db, before, limit)

    async def conversation_runs(
        self,
        principal: Principal,
        session_id: UUID,
        before: UUID | None,
        limit: int,
    ) -> AgentConversationPage:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            return await self._store(principal).conversation_runs(db, session_id, before, limit)

    async def execution(self, principal: Principal, run_id: UUID) -> AgentExecution:
        async with self._sessions.begin() as db:
            administrator = await self._identity.require_active_session(db, principal)
            execution = await self._store(principal).execution_in(db, run_id)
            if execution.partial_answer or execution.partial_reasoning:
                try:
                    if execution.chat:
                        await self._require_chat_ceiling(
                            db, principal, execution.chat, administrator, include_source=False
                        )
                    if execution.run.configuration_digest != _BASIC_CHAT_DIGEST:
                        await self.skills.require_execution_configuration(
                            db,
                            principal,
                            execution.run.skill_version_id,
                            execution.run.configuration_digest,
                        )
                except (HintCeilingDenied, SkillBindingUnavailable, SkillVersionUnavailable):
                    return execution.model_copy(
                        update={"partial_answer": None, "partial_reasoning": None}
                    )
            return await self._project_execution(db, principal, execution)

    async def _project_execution(
        self, db: AsyncSession, principal: Principal, execution: AgentExecution
    ) -> AgentExecution:
        draft = execution.result
        if not isinstance(draft, FocusedTrainingResult) or draft.training_set is None:
            return execution
        saved = await LearningService.saved_agent_plan_id(db, principal, execution.run.run_id)
        if saved is not None:
            return execution.model_copy(update={"training_id": saved})
        if await self._training_draft_is_current(db, draft):
            return execution
        changed = (
            "题库已变更。请按需要重新找题。"
            if draft.training_set.search is not None
            else "题库已变更。请调整条件重新生成，或新建训练集。"
        )
        return execution.model_copy(
            update={
                "result": draft.model_copy(
                    update={
                        "items": (),
                        "replacement": None,
                        "goal_coverage": None,
                        "model_hint": None,
                        "model_status": None,
                        "gaps": (changed,),
                    }
                )
            }
        )

    async def cancel(self, principal: Principal, run_id: UUID) -> AgentExecution:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            await AgentQueue(self._sessions).cancel(db, principal.account_id, run_id)
            execution = await self._store(principal).execution_in(db, run_id)
            return await self._project_execution(db, principal, execution)

    async def save_feedback(
        self, principal: Principal, run_id: UUID, body: UpdateRunFeedback
    ) -> AgentExecution:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            execution = await self._store(principal).save_feedback(db, run_id, body)
            return await self._project_execution(db, principal, execution)

    async def save_plan(self, principal: Principal, run_id: UUID) -> TrainingDetail:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            execution = await self._store(principal).execution_in(db, run_id, lock=True)
            if execution.run.state != RunState.SUCCEEDED or not isinstance(
                execution.result, (FocusedTrainingResult, PeriodicReviewResult)
            ):
                raise LearningUnavailable("learning run has no available training draft")
            if (
                isinstance(execution.result, FocusedTrainingResult)
                and execution.result.training_set is not None
                and execution.result.training_set.search is not None
            ):
                raise LearningUnavailable("problem search is not a training draft")

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
                if (
                    (isinstance(result, FocusedTrainingResult) and result.training_set is not None)
                    or execution.periodic_review is not None
                )
                else None,
            )
            return await service.save_agent_plan(db, principal, run_id, result, config.parameters)

    async def _review_resource_access(
        self,
        db: AsyncSession,
        principal: Principal,
        running: AgentRun,
        resource: ReviewResourceName,
    ) -> None:
        await self._identity.require_active_session(db, principal)
        await require_agent_access(db, principal.account_id)
        grant = await self.skills.for_current_skill(
            db, principal, running.skill_version_id, running.configuration_digest
        )
        key = SOURCE_EVIDENCE if resource == "code" else SUBMISSION_HISTORY
        if grant is None or key not in grant.tools:
            raise SkillBindingUnavailable("review resource unavailable")
        await self._store(principal).require_running(db, running)

    def _review_resources(
        self,
        principal: Principal,
        running: AgentRun,
        result: SubmissionReviewResult,
        facts: ReviewModelFacts,
    ) -> dict[ReviewResourceName, Callable[[], Awaitable[str]]]:
        anchor = result.records[0]
        code = review_model_facts(result, include_code_counts=True)

        async def evaluations() -> str:
            async with self._sessions.begin() as db:
                await self._review_resource_access(db, principal, running, "evaluations")
                if anchor.submission_id != result.anchor_submission_id:
                    raise LookupError("selected evaluation unavailable")
                if not await reserve_review_evaluation_read(
                    db, principal.account_id, anchor.problem_version_id
                ):
                    raise LookupError("evaluation summary unavailable")
                category = await owned_review_evaluation_category(
                    db,
                    principal,
                    submission_id=anchor.submission_id,
                    evaluation_id=anchor.evaluation_id,
                    problem_version_id=anchor.problem_version_id,
                    as_of=result.evidence_as_of or running.created_at,
                )
            evidence = facts.model_dump(mode="json", exclude={"question", "code", "focus_options"})
            evidence["selected_evaluation"] = ReviewEvaluationSummary(category=category).model_dump(
                mode="json"
            )
            return json.dumps(evidence, ensure_ascii=False)

        async def problem() -> str:
            async with self._sessions.begin() as db:
                await self._review_resource_access(db, principal, running, "problem")
                public = await problems.learning_review_problem(db, anchor.problem_version_id)
            if public is None:
                raise LookupError("public statement unavailable")
            return json.dumps(public, ensure_ascii=False)

        async def code_summary() -> str:
            async with self._sessions.begin() as db:
                await self._review_resource_access(db, principal, running, "code")
            if code is None or code.code is None:
                raise LookupError("code evidence unavailable")
            return code.code.model_dump_json(exclude_none=True)

        resources: dict[ReviewResourceName, Callable[[], Awaitable[str]]] = {
            "evaluations": evaluations,
            "problem": problem,
        }
        if code is not None and code.code is not None:
            resources["code"] = code_summary
        if self._review_batch_offers and result.anchor_submission_id == anchor.submission_id:

            async def batches() -> str:
                offers, outcomes = await self._review_batches(principal, running, result)
                return json.dumps(
                    {
                        "options": [
                            {"title": offer.title, "purpose": offer.purpose} for offer in offers
                        ],
                        "results": [row.model_dump(mode="json") for row in outcomes],
                    },
                    ensure_ascii=False,
                )

            resources["batch_diagnostics"] = batches
        return resources

    async def _review_batches(
        self, principal: Principal, running: AgentRun, result: SubmissionReviewResult
    ) -> tuple[tuple[RegisteredBatchOffer, ...], tuple[ReviewBatchOutcome, ...]]:
        async with self._sessions.begin() as db:
            await self._review_resource_access(db, principal, running, "batch_diagnostics")
            view = await db.scalar(
                select(func.kf_review_batch_context(running.run_id, running.revision))
            )
        if view is None:
            return (), ()
        anchor = result.records[0]
        offers = {
            offer.manifest_sha256: offer
            for offer in self._review_batch_offers
            if offer.problem_version_id == anchor.problem_version_id
            and offer.language == view["language"]
        }
        outcomes = tuple(
            ReviewBatchOutcome(purpose=row["purpose"], outcome=row["outcome"])
            for row in view["outcomes"]
            if (offer := offers.get(row["manifest_sha256"])) is not None
            and row["purpose"] == offer.purpose
        )
        return tuple(offers.values())[:8], outcomes

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
        *,
        chat_source: SourceExcerptModelContext | None = None,
        chat: LearningChatRequest | None = None,
    ) -> WithHint:
        source_context = content if isinstance(content, SourceExcerptModelContext) else chat_source
        model = self._model
        if source_context is not None and not getattr(model, "accepts_source_excerpt", False):
            model = None
        practice_tool_only = chat is not None and chat.practice_tool_only
        if practice_tool_only and isinstance(content, GuidedChatModelFacts):
            content = content.model_copy(update={"recent_practice": ()})
        practice = content.recent_practice if isinstance(content, GuidedChatModelFacts) else ()
        practice_in_context = bool(practice)
        practice_granted = False
        chat_model = (
            model if isinstance(model, ChatResourceModel) and model.supports_review_mcp else None
        )

        async def verify_practice(db: AsyncSession) -> None:
            await self.skills.require_execution_configuration(
                db,
                principal,
                running.skill_version_id,
                running.configuration_digest,
                required_tool=SUBMISSION_HISTORY,
            )

        async def verify_chat(db: AsyncSession) -> None:
            administrator = await self._identity.require_active_session(db, principal)
            if chat is not None:
                include_source = chat_source is not None
                await self._require_chat_ceiling(
                    db, principal, chat, administrator, include_source=include_source
                )
                await self._require_chat_evidence_grants(
                    db,
                    principal,
                    running.skill_version_id,
                    running.configuration_digest,
                    chat,
                    include_source=include_source,
                )
            if practice_in_context:
                await verify_practice(db)

        async def verify(db: AsyncSession) -> None:
            await require_agent_access(db, principal.account_id)
            if running.configuration_digest != _BASIC_CHAT_DIGEST:
                await self.skills.require_execution_configuration(
                    db, principal, running.skill_version_id, running.configuration_digest
                )

        if (
            isinstance(content, GuidedChatModelFacts)
            and model is not None
            and running.configuration_digest != _BASIC_CHAT_DIGEST
        ):
            async with self._sessions.begin() as db:
                await verify_chat(db)
                grant = await self.skills.for_current_skill(
                    db, principal, running.skill_version_id, running.configuration_digest
                )
                practice_granted = grant is not None and SUBMISSION_HISTORY in grant.tools
                if (
                    practice_granted
                    and chat_model is None
                    and not practice
                    and not practice_tool_only
                ):
                    await require_agent_access(db, principal.account_id)
                    await storage.require_running(db, running)
                    practice = await self._recent_practice_facts(db, principal, running.created_at)
                    practice_in_context = bool(practice)
                    content = content.model_copy(update={"recent_practice": practice})

        previous_invocations = ()
        discarded_invocations = ()
        choices = ()
        guarded_diagnostic = False
        review_resources = None
        if isinstance(content, GuidedChatModelFacts):
            invocation = "responses-guided-chat-v1"
            level = chat.bounded_hint_level() if chat is not None else None
            guarded_diagnostic = level == "L3" and chat_source is not None
            if not guarded_diagnostic:
                choices = hint_choices(content, level)
            if choices or guarded_diagnostic:
                discarded_invocations = (invocation,)
                invocation = (
                    "responses-guided-diagnostic-v1"
                    if guarded_diagnostic
                    else "responses-guided-choice-v1"
                )
                content = content.model_copy(update={"hint_level": level, "hint_choices": choices})
            if chat is not None and chat.include_submission_details:
                discarded_invocations += (invocation,)
                invocation = invocation.removesuffix("-v1") + "-v2"
            raw = content.model_dump_json(exclude_none=True).encode()
            if chat_source is not None:
                raw += chat_source.model_dump_json().encode()
        elif source_context is not None:
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
            if content.question is not None and isinstance(result, SubmissionReviewResult):
                previous_invocations += ("responses-review-mcp-v1", "responses-review-mcp-v2")
                if isinstance(model, ReviewResourceModel) and model.supports_review_mcp:
                    review_resources = self._review_resources(principal, running, result, content)
                    previous_invocations += (invocation,)
                    invocation = "responses-review-mcp-v2"
        elif isinstance(content, GuidedHelpModelFacts):
            invocation = "responses-concept-verdict-v1"
            raw = content.model_dump_json().encode()
        elif isinstance(content, PlanModelFacts):
            invocation = "responses-plan-explanation-v1"
            raw = content.model_dump_json(exclude_none=True).encode()
        else:
            invocation = "responses-concept-v1"
            raw = content.encode()
        open_chat = (
            isinstance(content, GuidedChatModelFacts) and not choices and not guarded_diagnostic
        )
        typed_review = (
            isinstance(content, (ReviewModelFacts, SourceExcerptModelContext))
            and content.question is not None
        )
        request_seconds = (
            CHAT_MODEL_REQUEST_SECONDS
            if open_chat
            else REVIEW_MODEL_REQUEST_SECONDS
            if typed_review
            else MODEL_REQUEST_SECONDS
        )
        try:
            async with self._sessions.begin() as db:
                await verify_chat(db)
                step, dispatch, checkpoint = await storage.begin_model_step(
                    db,
                    running,
                    invocation,
                    _digest(raw),
                    self._model_limits if model is not None else None,
                    verify,
                    request_seconds=request_seconds,
                    previous_invocations=previous_invocations,
                    discarded_invocations=discarded_invocations,
                )
                if dispatch and source_context is not None:
                    excerpt = await storage.take_source_excerpt(db, running)
                    if excerpt is None:
                        raise ModelHintUnavailable("source excerpt unavailable")
                    source = SourceExcerptModelFacts(
                        **source_context.model_dump(), text=excerpt.decode("utf-8")
                    )
                    content = (
                        content.model_copy(update={"source_excerpt": source})
                        if isinstance(content, GuidedChatModelFacts)
                        else source
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
            status = "uncertain"
            if step.state == "failed":
                status = self._model_failure_status(content, step.error_code)
            return result.model_copy(update={"model_status": status})
        assert model is not None

        async def preview(channel: ModelOutputChannel, text: str) -> None:
            await storage.publish_chat_output(running, channel, text, verify=verify_chat)

        async def read_practice() -> str:
            nonlocal practice_in_context
            async with self._sessions.begin() as db:
                await verify_chat(db)
                await require_agent_access(db, principal.account_id)
                await storage.require_running(db, running)
                if not practice_in_context:
                    await verify_practice(db)
                facts = practice or await self._recent_practice_facts(
                    db, principal, running.created_at
                )
                practice_in_context = True
            return json.dumps(
                {"recent_practice": [item.model_dump(mode="json") for item in facts]},
                ensure_ascii=False,
            )

        async def continue_request(resources: tuple[ReviewResourceName, ...]) -> bool:
            async def verify_resources(db: AsyncSession) -> None:
                await verify_chat(db)
                await verify(db)
                if review_resources is not None:
                    for resource in resources:
                        await self._review_resource_access(db, principal, running, resource)

            return await storage.begin_model_continuation(
                running, self._model_limits, verify_resources
            )

        async def review_preview(channel: ModelOutputChannel, text: str) -> None:
            if (
                channel == "answer"
                and review_resources is None
                and (
                    isinstance(content, SourceExcerptModelFacts)
                    or (
                        isinstance(content, ReviewModelFacts)
                        and (
                            content.focus_options
                            or (content.code and content.code.comparison_cases)
                        )
                    )
                )
            ):
                return

            async def verify_preview(db: AsyncSession) -> None:
                await verify_chat(db)
                await verify(db)

            await storage.publish_chat_output(running, channel, text, verify=verify_preview)

        try:
            if (
                open_chat
                and isinstance(content, GuidedChatModelFacts)
                and practice_granted
                and chat_model is not None
            ):
                hint = await chat_model.chat_with_tools(
                    content.model_copy(update={"recent_practice": ()}),
                    preview,
                    read_practice,
                    continue_request,
                )
            elif (
                open_chat
                and isinstance(content, GuidedChatModelFacts)
                and isinstance(model, StreamingLearningHintModel)
            ):
                hint = await model.stream_hint(content, preview)
            elif review_resources is not None:
                assert isinstance(content, ReviewModelFacts)
                assert isinstance(model, ReviewResourceModel)

                if "batch_diagnostics" in review_resources:
                    assert isinstance(result, SubmissionReviewResult)
                    _, outcomes = await self._review_batches(principal, running, result)
                    content = content.model_copy(update={"batch_diagnostics": outcomes})
                if isinstance(model, StreamingReviewModel):
                    hint = await model.stream_review(
                        content,
                        review_preview,
                        resources=review_resources,
                        continue_request=continue_request,
                    )
                else:
                    hint = await model.review_with_tools(
                        content, review_resources, continue_request
                    )
            elif (
                typed_review
                and isinstance(content, (ReviewModelFacts, SourceExcerptModelFacts))
                and isinstance(model, StreamingReviewModel)
            ):
                hint = await model.stream_review(content, review_preview)
            else:
                hint = await model.hint(content)
            if choices or guarded_diagnostic:
                source_line = None
                try:
                    if guarded_diagnostic:
                        assert isinstance(content, GuidedChatModelFacts)
                        assert content.source_excerpt is not None
                        diagnostic = GuidedDiagnosticChoice.model_validate_json(hint.text)
                        selected = local_diagnostic(content.source_excerpt, diagnostic)
                        source_line = diagnostic.line
                    else:
                        pick = GuidedHintChoice.model_validate_json(hint.text).choice
                        selected = choices[pick - 1]
                except (ValueError, IndexError):
                    raise ModelHintUnavailable(
                        "model selected an unavailable guided reply",
                        code="model_invalid_response",
                        usage=hint.usage,
                    ) from None
                hint = ModelHint(
                    text=selected,
                    model=hint.model,
                    response_id=hint.response_id,
                    latency_ms=hint.latency_ms,
                    usage=hint.usage,
                    source_line=source_line,
                )
            if isinstance(content, SourceExcerptModelFacts) and (
                hint.source_line is None
                or not content.start_line <= hint.source_line <= content.end_line
            ):
                raise ModelHintUnavailable(
                    "invalid source analysis citation",
                    code="model_invalid_response",
                    usage=hint.usage,
                )
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
                verify=verify_chat if chat is not None else None,
            )
        except (HintCeilingDenied, AuthenticationFailed, SkillBindingUnavailable) as error:
            await storage.record_step(
                step.model_copy(update={"state": "failed", "error_code": _failure_code(error)}),
                running.revision,
            )
            raise
        except ModelHintUnavailable as error:
            await storage.record_step(
                step.model_copy(update={"state": "failed", "error_code": error.code}),
                running.revision,
                model_usage=error.usage,
            )
            status = self._model_failure_status(content, error.code)
            return result.model_copy(update={"model_status": status})
        return result.model_copy(update={"model_hint": hint, "model_status": "generated"})

    @staticmethod
    def _model_failure_status(
        content: ModelHintInput | SourceExcerptModelContext, code: str | None
    ):
        if code == "model_output_limit" and isinstance(
            content, (GuidedChatModelFacts, ReviewModelFacts, SourceExcerptModelContext)
        ):
            return "output_limit"
        if code == "model_budget_exhausted":
            return "budget_exhausted"
        return "context_limit" if code == "model_context_limit" else "unavailable"

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
            await self.check_contest_access(principal)
            administrator = await self._authenticate(principal)
            body = await storage.workflow_input(running)
            tools = scoped_agent_tools(
                self._sessions,
                principal,
                self._identity,
                self.skills,
                source_evidence=body.source_evidence,
                as_of=body.evidence_as_of or running.created_at,
            )
            required_tool = (
                TRAINING_CANDIDATES
                if body.training_set is not None or body.periodic_review is not None
                else None
            )
            if running.configuration_digest == _BASIC_CHAT_DIGEST:
                if body.chat is None or body.chat.include_submission_details or body.chat_source:
                    raise SkillBindingUnavailable("plain chat cannot read submission evidence")
                parameters = GuidedHelpParameters()
            else:
                config = await self.skills.configuration(
                    principal,
                    running.skill_version_id,
                    running.configuration_digest,
                    required_tool=required_tool,
                )
                parameters = config.parameters
            if isinstance(parameters, GuidedHelpParameters) and body.chat is None:
                self._require_legacy_guided_ceiling(body, administrator)
            workflow = LearningWorkflow(invoke)
            try:
                training_source = await self._training_source(
                    storage, running.session_id, running.skill_version_id, body.training_set
                )
            except (AgentRecordUnavailable, ValueError) as error:
                raise LearningEvidenceUnavailable("training set draft unavailable") from error
            result = await workflow.run(
                parameters,
                body,
                review_anchor=body.review_anchor,
                training_source=training_source,
            )
            if body.chat is not None and isinstance(result, GuidedHelpResult):
                chat_facts = body.chat_context
                if chat_facts is None:
                    raise LearningEvidenceUnavailable("chat context unavailable")
                result = await self._add_model_hint(
                    principal,
                    storage,
                    running,
                    result,
                    chat_facts,
                    ordinal + 1,
                    chat_source=body.chat_source,
                    chat=body.chat,
                )
            elif body.model_topic is not None and isinstance(result, GuidedHelpResult):
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
                source_context = body.review_source
                if (
                    source_context is None
                    and result.code is not None
                    and result.code.source_available
                ):
                    source_context = SourceExcerptModelContext(
                        **body.model_source_excerpt.model_dump(),
                        language=result.code.source.language,
                    )
                if source_context is None:
                    result = result.model_copy(update={"model_status": "insufficient_evidence"})
                else:
                    result = await self._add_model_hint(
                        principal,
                        storage,
                        running,
                        result,
                        source_context,
                        ordinal + 1,
                    )
            elif (
                body.include_model_review
                and isinstance(result, SubmissionReviewResult)
                and isinstance(parameters, SubmissionReviewParameters)
            ):
                targets = review_focus_targets(result) if body.include_model_evidence_focus else {}
                facts = review_model_facts(
                    result,
                    include_code_counts=body.include_model_code_context,
                    include_control_flow=body.include_model_control_flow,
                    include_comparison_cases=body.include_model_comparison_cases,
                    focus_options=tuple(targets),
                    question=body.review_question,
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
                    result = result.model_copy(
                        update={"model_review_basis": basis, "evidence_as_of": body.evidence_as_of}
                    )
                    result = await self._add_model_hint(
                        principal, storage, running, result, facts, ordinal + 1
                    )
                    choice = result.model_hint.review_focus if result.model_hint else None
                    if choice is not None and (target := targets.get(choice)) is not None:
                        ordinal += 1
                        sequence = await storage.last_step_sequence(running)
                        try:
                            verified = await workflow.verify_review_evidence(parameters, target)
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
                await require_agent_access(db, principal.account_id)
                administrator = await self._identity.require_active_session(db, principal)
                if body.chat is not None:
                    await self._require_chat_ceiling(
                        db,
                        principal,
                        body.chat,
                        administrator,
                        include_source=body.chat_source is not None,
                    )
                    await self._require_chat_evidence_grants(
                        db,
                        principal,
                        running.skill_version_id,
                        running.configuration_digest,
                        body.chat,
                        include_source=body.chat_source is not None,
                    )
                elif isinstance(result, GuidedHelpResult):
                    self._require_legacy_guided_ceiling(body, administrator)

                async def verify(actual: AgentRun) -> None:
                    if actual.configuration_digest != _BASIC_CHAT_DIGEST:
                        await self.skills.require_execution_configuration(
                            db,
                            principal,
                            actual.skill_version_id,
                            actual.configuration_digest,
                            required_tool=required_tool,
                        )

                result = result.model_copy(update={"evidence_as_of": body.evidence_as_of})
                assisted_version = (
                    body.chat.assisted_version()
                    if body.chat is not None
                    and isinstance(result, GuidedHelpResult)
                    and result.model_status == "generated"
                    and result.model_hint is not None
                    and result.model_hint.text != LOCAL_SOURCE_REQUEST
                    else None
                )
                return await storage.finish_execution(
                    db, running, result=result, assisted_version=assisted_version, verify=verify
                )
        except _EXPECTED_FAILURES as error:
            async with self._sessions.begin() as db:
                if isinstance(error, ToolTimedOut) and error.retryable:
                    deferred = await AgentQueue(self._sessions).defer(db, running)
                    if deferred is not None:
                        return AgentExecution(run=deferred)
                return await storage.finish_execution(
                    db, running, failure_code=_failure_code(error)
                )
