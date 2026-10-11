"""Local-only composition for fixed database authority operations."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.modules.identity.passwords import USERNAME

AuthorityOperation = Literal["bootstrap", "grant", "revoke", "enable", "disable"]


@dataclass(frozen=True)
class AuthorityResult:
    account_id: UUID
    username: str
    enabled: bool
    is_administrator: bool
    authorization_version: int
    changed: bool


async def manage_authority(
    sessions: async_sessionmaker, username: str, operation: AuthorityOperation
) -> AuthorityResult:
    if not USERNAME.fullmatch(username) or operation not in (
        "bootstrap",
        "grant",
        "revoke",
        "enable",
        "disable",
    ):
        raise ValueError("invalid authority operation")
    try:
        async with sessions.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout='5s'"))
            await session.execute(text("SET LOCAL lock_timeout='2s'"))
            # The CLI must use a dedicated, unprivileged local operator login.
            # Web/platform, import and coordinator credentials are never reused.
            safe = await session.scalar(
                text("""
                SELECT NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
                    OR rolbypassrls)
                    AND rolcanlogin AND pg_has_role(current_user,'kf_oj_identity_operator','USAGE')
                    AND NOT pg_has_role(current_user,'kf_oj_platform','MEMBER')
                    AND NOT pg_has_role(current_user,'kf_oj_coordinator','MEMBER')
                    AND NOT pg_has_role(current_user,'kf_oj_problem_import','MEMBER')
                FROM pg_roles WHERE rolname=current_user
            """)
            )
            if not safe:
                raise ValueError("a dedicated unprivileged identity operator login is required")
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM public.kf_manage_account_authority(:username,:operation)"
                        ),
                        {"username": username, "operation": operation},
                    )
                )
                .mappings()
                .one()
            )
            return AuthorityResult(**row)
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None)
        messages = {
            "P0002": "account not found",
            "23514": (
                "authority change rejected; preserve an enabled administrator "
                "and use explicit bootstrap"
            ),
            "40001": "concurrent authority change rejected; retry the whole operation",
            "55P03": "authority operation busy; retry later",
            "57014": "authority operation timed out; verify current state before retrying",
            "42501": "identity operator permissions unavailable",
        }
        # Never print a connection URL, SQL bind values or driver diagnostics.
        raise ValueError(messages.get(code, "authority database operation unavailable")) from None
