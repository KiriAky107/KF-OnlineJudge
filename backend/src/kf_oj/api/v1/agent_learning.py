"""Personal learning skills and runtime endpoints using the platform session."""

import json
from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.agent_api import (
    AgentConversationPage,
    AgentHistoryPage,
    AgentRunTrace,
    AgentRunView,
    ConfigureSkill,
    ConversationPage,
    CreateLearningSession,
    LearningSessionView,
    SkillBindingView,
    SkillCatalogView,
    SkillToolView,
    SkillVersionView,
    StartAgentRun,
)
from kf_oj.contracts.agent_learning import AgentExecution, AgentLearningInput, UpdateRunFeedback
from kf_oj.contracts.learning import TrainingDetail
from kf_oj.contracts.learning_model import LearningHintCeiling
from kf_oj.modules.contests.public import AgentUnavailableDuringContest
from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.learning.agent_application import AgentLearning
from kf_oj.modules.learning.hint_policy import HintCeilingDenied
from kf_oj.modules.learning.public import LearningQueueBusy, LearningUnavailable, NoCandidates
from kf_oj.modules.skills.public import (
    RuntimeDeclaration,
    SkillBinding,
    SkillBindingUnavailable,
    SkillVersionUnavailable,
)
from kf_oj.runtime.storage import AgentRecordConflict, AgentRecordUnavailable
from kf_oj.runtime.tools import ToolKey

router = APIRouter(prefix="/learning/agent", tags=["learning-agent"])


def learning_agent(request: Request) -> AgentLearning:
    service = getattr(request.app.state, "agent_learning", None)
    if service is None:
        raise HTTPException(503, "learning agent unavailable")
    return service


Agent = Annotated[AgentLearning, Depends(learning_agent)]


async def _contest_allowed(principal: Principal, agent: Agent) -> Principal:
    try:
        await agent.check_contest_access(principal)
    except AgentUnavailableDuringContest:
        raise HTTPException(403, "learning agent paused during contest") from None
    return principal


async def _reader(principal: Annotated[Principal, Depends(current_principal)], agent: Agent):
    return await _contest_allowed(principal, agent)


async def _writer(principal: Annotated[Principal, Depends(unsafe_principal)], agent: Agent):
    return await _contest_allowed(principal, agent)


Reader = Annotated[Principal, Depends(_reader)]
Writer = Annotated[Principal, Depends(_writer)]


async def _action[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except HintCeilingDenied:
        raise HTTPException(403, "hint level unavailable") from None
    except (SkillBindingUnavailable, SkillVersionUnavailable, AgentRecordUnavailable):
        raise HTTPException(404, "learning resource unavailable") from None
    except (LearningUnavailable, AgentRecordConflict):
        raise HTTPException(409, "learning request conflicts with current state") from None
    except LearningQueueBusy:
        raise HTTPException(429, "learning queue busy") from None
    except NoCandidates:
        raise HTTPException(422, "no available matching problems") from None


def _tools(keys: frozenset[ToolKey]) -> tuple[SkillToolView, ...]:
    return tuple(
        SkillToolView(name=key.name, version=key.version)
        for key in sorted(keys, key=lambda key: (key.name, key.version))
    )


def _binding(binding: SkillBinding) -> SkillBindingView:
    return SkillBindingView(
        skill_version_id=binding.version_id,
        enabled=binding.enabled,
        parameters=binding.parameters.model_dump(mode="json"),
        authorized_tools=_tools(binding.authorized_tools),
    )


def _run(execution: AgentExecution) -> AgentRunView:
    return AgentRunView(
        run_id=execution.run.run_id,
        session_id=execution.run.session_id,
        skill_version_id=execution.run.skill_version_id,
        state=execution.run.state,
        created_at=execution.run.created_at,
        result=execution.result,
        partial_answer=execution.partial_answer,
        partial_reasoning=execution.partial_reasoning,
        training_request=execution.training_request,
        chat=execution.chat,
        review_question=execution.review_question,
        periodic_review=execution.periodic_review,
        chat_history=execution.chat_history,
        failure_code=execution.failure_code,
        feedback=execution.feedback,
        training_id=execution.training_id,
    )


@router.get("/skills", response_model=SkillCatalogView)
async def skills(
    agent: Agent, principal: Reader, version_id: UUID | None = None
) -> SkillCatalogView:
    personal = await _action(agent.skills.catalog_versions(principal, version_id))
    versions = []
    for skill, binding in personal:
        declaration = skill.declaration
        versions.append(
            SkillVersionView(
                version_id=skill.version_id,
                origin=declaration.origin
                if isinstance(declaration, RuntimeDeclaration)
                else "builtin",
                slug=declaration.slug,
                release=declaration.release,
                title=declaration.title,
                description=declaration.description,
                purpose=declaration.purpose,
                default_parameters=declaration.parameters.model_dump(mode="json"),
                available_tools=_tools(agent.skills.approved_tools(skill)),
                binding=_binding(binding) if binding is not None else None,
                archived_at=declaration.archived_at
                if isinstance(declaration, RuntimeDeclaration)
                else None,
            )
        )
    return SkillCatalogView(versions=tuple(versions))


@router.put("/skills/{version_id}", response_model=SkillBindingView)
async def configure_skill(
    version_id: UUID, body: ConfigureSkill, agent: Agent, principal: Writer
) -> SkillBindingView:
    try:
        authorized = frozenset(ToolKey(tool.name, tool.version) for tool in body.authorized_tools)
        if len(authorized) != len(body.authorized_tools):
            raise ValueError("duplicate skill tools")
        return _binding(
            await _action(
                agent.skills.configure(
                    principal,
                    version_id,
                    parameters=json.dumps(body.parameters, ensure_ascii=False).encode(),
                    enabled=body.enabled,
                    authorized_tools=authorized,
                )
            )
        )
    except ValueError:
        raise HTTPException(422, "invalid skill configuration") from None


@router.post("/sessions", response_model=LearningSessionView, status_code=201)
async def new_session(
    body: CreateLearningSession, agent: Agent, principal: Writer
) -> LearningSessionView:
    session = await _action(
        agent.create_session(
            principal, body.purpose, conversation_session_id=body.conversation_session_id
        )
    )
    return LearningSessionView(
        purpose=session.purpose, session_id=session.session_id, created_at=session.created_at
    )


@router.get("/hint-ceiling", response_model=LearningHintCeiling)
async def hint_ceiling(
    agent: Agent, principal: Reader, problem_version_id: UUID, training_id: UUID | None = None
) -> LearningHintCeiling:
    return await _action(agent.hint_ceiling(principal, problem_version_id, training_id))


@router.post("/sessions/{session_id}/runs", response_model=AgentRunView, status_code=202)
async def start_run(
    session_id: UUID, body: StartAgentRun, agent: Agent, principal: Writer
) -> AgentRunView:
    try:
        return _run(
            await _action(
                agent.prepare(
                    principal,
                    session_id,
                    body.request_id,
                    body.skill_version_id,
                    AgentLearningInput(
                        submission_id=body.submission_id,
                        include_source_evidence=body.include_source_evidence,
                        model_topic=body.model_topic,
                        include_model_verdict=body.include_model_verdict,
                        include_model_review=body.include_model_review,
                        include_model_code_context=body.include_model_code_context,
                        include_model_control_flow=body.include_model_control_flow,
                        include_model_comparison_cases=body.include_model_comparison_cases,
                        include_model_evidence_focus=body.include_model_evidence_focus,
                        include_model_plan_explanation=body.include_model_plan_explanation,
                        model_source_excerpt=body.model_source_excerpt,
                        training_set=body.training_set,
                        chat=body.chat,
                        review_question=body.review_question,
                        periodic_review=body.periodic_review,
                    ),
                )
            )
        )
    except ValueError:
        raise HTTPException(422, "invalid learning run input") from None


@router.get("/runs", response_model=AgentHistoryPage)
async def run_history(
    agent: Agent,
    principal: Reader,
    group_chat: bool = False,
    problem_version_id: UUID | None = None,
    training_drafts: bool = False,
) -> AgentHistoryPage:
    return await _action(
        agent.history(
            principal,
            group_chat=group_chat,
            problem_version_id=problem_version_id,
            training_drafts=training_drafts,
        )
    )


@router.get("/conversations", response_model=AgentHistoryPage)
async def recent_conversations(
    agent: Agent, principal: Reader, problem_version_id: UUID | None = None
) -> AgentHistoryPage:
    return await _action(agent.recent_conversations(principal, problem_version_id))


@router.get("/sessions/{session_id}/conversation", response_model=AgentConversationPage)
async def conversation_runs(
    session_id: UUID,
    agent: Agent,
    principal: Reader,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=30)] = 12,
) -> AgentConversationPage:
    return await _action(agent.conversation_runs(principal, session_id, before, limit))


@router.get("/runs/{run_id}/conversation", response_model=ConversationPage)
async def conversation(
    run_id: UUID, agent: Agent, principal: Reader, limit: Annotated[int, Query(ge=1, le=12)] = 6
) -> ConversationPage:
    return await _action(agent.conversation(principal, run_id, limit))


@router.get("/runs/{run_id}", response_model=AgentRunView)
async def read_run(run_id: UUID, agent: Agent, principal: Reader) -> AgentRunView:
    return _run(await _action(agent.execution(principal, run_id)))


@router.get("/runs/{run_id}/trace", response_model=AgentRunTrace)
async def run_trace(run_id: UUID, agent: Agent, principal: Reader) -> AgentRunTrace:
    return await _action(agent.trace(principal, run_id))


@router.put("/runs/{run_id}/feedback", response_model=AgentRunView)
async def save_feedback(
    run_id: UUID, body: UpdateRunFeedback, agent: Agent, principal: Writer
) -> AgentRunView:
    return _run(await _action(agent.save_feedback(principal, run_id, body)))


@router.post("/runs/{run_id}/cancel", response_model=AgentRunView)
async def cancel_run(run_id: UUID, agent: Agent, principal: Writer) -> AgentRunView:
    return _run(await _action(agent.cancel(principal, run_id)))


@router.post("/runs/{run_id}/training", response_model=TrainingDetail)
async def save_training(run_id: UUID, agent: Agent, principal: Writer) -> TrainingDetail:
    return await _action(agent.save_plan(principal, run_id))
