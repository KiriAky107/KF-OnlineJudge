from typing import Annotated

from fastapi import Depends, HTTPException, Request

from kf_oj.core.settings import Settings
from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.identity.service import IdentityService

SESSION_COOKIE = "kf_session"


def identity_service(request: Request) -> IdentityService:
    service = getattr(request.app.state, "identity", None)
    if service is None:
        raise HTTPException(503, "identity service unavailable")
    return service


def trusted_origin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if request.headers.get("origin") not in settings.browser_origins:
        raise HTTPException(403, "request origin rejected")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "cross-site request rejected")


async def current_principal(
    request: Request, identity: Annotated[IdentityService, Depends(identity_service)]
) -> Principal:
    try:
        return await identity.authenticate(request.cookies.get(SESSION_COOKIE))
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None


async def unsafe_principal(
    request: Request, identity: Annotated[IdentityService, Depends(identity_service)]
) -> Principal:
    trusted_origin(request)
    try:
        await identity.authenticate(request.cookies.get(SESSION_COOKIE))
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    csrf = request.headers.get("x-csrf-token")
    if csrf is None:
        raise HTTPException(403, "request token required")
    try:
        return await identity.authenticate(request.cookies.get(SESSION_COOKIE), csrf_token=csrf)
    except AuthenticationFailed:
        raise HTTPException(403, "request authentication rejected") from None
