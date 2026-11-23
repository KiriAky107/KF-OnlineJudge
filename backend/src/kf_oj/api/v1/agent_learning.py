"""Personal learning skills and runtime endpoints using the platform session."""

import json
from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.agent_api import (
    AgentHistoryPage,
    AgentRunTrace,
    AgentRunView,
    ConfigureSkill,
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
from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.learning.agent_application import AgentLearning
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

Reader = Annotated[Principal, Depends(current_principal)]
Writer = Annotated[Principal, Depends(unsafe_principal)]


def learning_agent(request: Request) -> AgentLearning:
    service = getattr(request.app.state, "agent_learning", None)
    if service is None:
        raise HTTPException(503, "learning agent unavailable")
    return service


Agent = Annotated[AgentLearning, Depends(learning_agent)]


async def _action[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
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
        failure_code=execution.failure_code,
        feedback=execution.feedback,
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
    session = await _action(agent.create_session(principal, body.purpose))
    return LearningSessionView(
        purpose=session.purpose, session_id=session.session_id, created_at=session.created_at
    )


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
                    ),
                )
            )
        )
    except ValueError:
        raise HTTPException(422, "invalid learning run input") from None


@router.get("/runs", response_model=AgentHistoryPage)
async def run_history(agent: Agent, principal: Reader) -> AgentHistoryPage:
    return await _action(agent.history(principal))


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
