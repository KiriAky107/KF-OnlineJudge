"""Cookie-authenticated personal skill authoring."""

from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.skill_drafts import (
    CreateSkillDraft,
    CreateSkillTrial,
    SkillDraftPage,
    SkillDraftView,
    SkillTrialView,
    UpdateSkillDraft,
)
from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.skills.public import (
    SkillDraftConflict,
    SkillDrafts,
    SkillDraftUnavailable,
    SkillVersionUnavailable,
)

router = APIRouter(prefix="/learning/agent/skill-drafts", tags=["skill-drafts"])
Reader = Annotated[Principal, Depends(current_principal)]
Writer = Annotated[Principal, Depends(unsafe_principal)]


def _service(request: Request) -> SkillDrafts:
    drafts = getattr(request.app.state, "skill_drafts", None)
    if drafts is None:
        raise HTTPException(503, "skill drafts unavailable")
    return drafts


Drafts = Annotated[SkillDrafts, Depends(_service)]


async def _action[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except (SkillDraftUnavailable, SkillVersionUnavailable):
        raise HTTPException(404, "skill unavailable") from None
    except SkillDraftConflict:
        raise HTTPException(409, "draft revision or request conflict") from None
    except ValueError:
        raise HTTPException(422, "invalid skill declaration") from None


@router.get("", response_model=SkillDraftPage)
async def list_drafts(drafts: Drafts, principal: Reader) -> SkillDraftPage:
    return await _action(drafts.page(principal))


@router.post("", response_model=SkillDraftView, status_code=201)
async def create_draft(body: CreateSkillDraft, drafts: Drafts, principal: Writer) -> SkillDraftView:
    return await _action(drafts.create(principal, body))


@router.get("/{draft_id}", response_model=SkillDraftView)
async def read_draft(draft_id: UUID, drafts: Drafts, principal: Reader) -> SkillDraftView:
    return await _action(drafts.get(principal, draft_id))


@router.put("/{draft_id}", response_model=SkillDraftView)
async def update_draft(
    draft_id: UUID,
    body: UpdateSkillDraft,
    drafts: Drafts,
    principal: Writer,
) -> SkillDraftView:
    return await _action(drafts.update(principal, draft_id, body))


@router.post("/{draft_id}/trials", response_model=SkillTrialView, status_code=201)
async def create_trial(
    draft_id: UUID,
    body: CreateSkillTrial,
    drafts: Drafts,
    principal: Writer,
) -> SkillTrialView:
    return await _action(drafts.create_trial(principal, draft_id, body.revision))
