from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from kf_oj.api.dependencies import (
    SESSION_COOKIE,
    current_principal,
    identity_service,
    trusted_origin,
    unsafe_principal,
)
from kf_oj.contracts.identity import CurrentUser, LoginRequest, LoginResponse
from kf_oj.modules.identity.public import AuthenticationFailed, LoginThrottled, Principal
from kf_oj.modules.identity.service import IdentityService

router = APIRouter(prefix="/auth", tags=["identity"])


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(trusted_origin)])
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    identity: Annotated[IdentityService, Depends(identity_service)],
) -> LoginResponse:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "JSON request required")
    try:
        issued = await identity.login(
            body.username,
            body.password.get_secret_value(),
            peer=request.client.host if request.client else "",
        )
    except LoginThrottled:
        raise HTTPException(
            429, "login temporarily limited", headers={"Retry-After": "900"}
        ) from None
    except (AuthenticationFailed, ValueError):
        raise HTTPException(401, "invalid credentials") from None
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        path="/api/v1",
    )
    response.set_cookie(
        "kf_csrf",
        issued.csrf_token,
        max_age=settings.session_lifetime_seconds,
        httponly=False,
        secure=settings.environment == "production",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(
        user=CurrentUser(id=issued.principal.account_id, username=issued.principal.username),
        expires_at=issued.expires_at,
        csrf_token=issued.csrf_token,
    )


@router.get("/me", response_model=CurrentUser)
async def me(response: Response, principal: Annotated[Principal, Depends(current_principal)]):
    response.headers["Cache-Control"] = "no-store"
    return CurrentUser(id=principal.account_id, username=principal.username)


@router.post("/logout", status_code=204, dependencies=[Depends(unsafe_principal)])
async def logout(
    request: Request, identity: Annotated[IdentityService, Depends(identity_service)]
) -> Response:
    try:
        await identity.logout(request.cookies[SESSION_COOKIE], request.headers["x-csrf-token"])
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        SESSION_COOKIE,
        path="/api/v1",
        httponly=True,
        secure=request.app.state.settings.environment == "production",
        samesite="strict",
    )
    response.delete_cookie(
        "kf_csrf",
        path="/",
        secure=request.app.state.settings.environment == "production",
        samesite="strict",
    )
    return response
