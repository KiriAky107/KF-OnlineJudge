from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.editor import EditorCompletion, EditorCompletionAccess, EditorCompletionRequest
from kf_oj.infra.clangd_completion import CompletionUnavailable
from kf_oj.modules.contests.public import blocks_semantic_completion
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.problems.public import editor_problem_titles

router = APIRouter(tags=["editor"])


async def completion_allowed(state, account_id, version_id):
    if getattr(state, "editor_completion", None) is None:
        return False
    async with state.sessions() as db:
        if version_id not in await editor_problem_titles(db, (version_id,)):
            return False
        return not await blocks_semantic_completion(db, account_id, version_id)


@router.get(
    "/problems/versions/{version_id}/editor-completion", response_model=EditorCompletionAccess
)
async def semantic_completion_access(
    version_id: UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    request: Request,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return EditorCompletionAccess(
        enabled=await completion_allowed(request.app.state, principal.account_id, version_id)
    )


@router.post(
    "/problems/versions/{version_id}/editor-completion", response_model=tuple[EditorCompletion, ...]
)
async def semantic_completion(
    version_id: UUID,
    body: EditorCompletionRequest,
    principal: Annotated[Principal, Depends(unsafe_principal)],
    request: Request,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    state = request.app.state
    if not await completion_allowed(state, principal.account_id, version_id):
        return ()
    slots = state.editor_completion_slots
    busy = state.editor_completion_busy
    if slots.locked() or principal.account_id in busy:
        return ()
    busy.add(principal.account_id)
    try:
        async with slots:
            items = await state.editor_completion.complete(
                body.language, body.source, body.line, body.character
            )
    except ValueError:
        raise HTTPException(422, "invalid editor source or position") from None
    except CompletionUnavailable:
        return ()
    finally:
        busy.discard(principal.account_id)
    if not await completion_allowed(state, principal.account_id, version_id):
        return ()
    return tuple(EditorCompletion(name=item.name, kind=item.kind) for item in items)
