"""Admission barrier while already accepted work retains its read/cancel paths."""

import re

from starlette.responses import JSONResponse

from kf_oj.infra.availability import current


def needs_open(method: str, path: str) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    if method == "POST" and path == "/api/v1/auth/logout":
        return False
    if method == "POST" and re.fullmatch(r"/api/v1/submissions/[0-9a-f-]{36}/cancel", path):
        return False
    if method == "PUT" and re.fullmatch(
        r"/api/v1/admin/problem-imports/[0-9a-f-]{36}/archive", path
    ):
        # The domain route still requires a previously authorized immutable
        # receipt. This cannot create a new upload or bypass its lease/epoch.
        return False
    return path.startswith("/api/v1/")


class AvailabilityMiddleware:
    def __init__(self, app, *, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and self.settings.environment == "production"
            and needs_open(scope["method"], scope.get("path", ""))
        ):
            try:
                state = current(
                    self.settings.delivery_release_id, self.settings.delivery_configuration_id
                )
                allowed = state.state == "open"
            except (OSError, ValueError, RecursionError):
                allowed = False
            if not allowed:
                await JSONResponse(
                    {"detail": "platform mutations unavailable"},
                    status_code=503,
                    headers={"Cache-Control": "no-store", "Retry-After": "10"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)
