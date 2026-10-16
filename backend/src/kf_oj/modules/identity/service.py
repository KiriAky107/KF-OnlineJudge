import asyncio
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.modules.identity import repository
from kf_oj.modules.identity.models import Account, Session
from kf_oj.modules.identity.passwords import (
    HASHER,
    hash_password,
    validate_account,
    verify_password,
)
from kf_oj.modules.identity.public import (
    AuthenticationFailed,
    LoginThrottled,
    MaintenanceDenied,
    Principal,
)

TOKEN = re.compile(r"[0-9a-f]{64}\Z")


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class IssuedSession:
    principal: Principal
    expires_at: datetime
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


class IdentityService:
    def __init__(self, sessions: async_sessionmaker, *, lifetime_seconds: int = 43200):
        if not 300 <= lifetime_seconds <= 86400:
            raise ValueError("invalid session lifetime")
        self.sessions = sessions
        self.lifetime_seconds = lifetime_seconds
        self._hash_slots = asyncio.Semaphore(2)
        self._dummy_hash = HASHER.hash(secrets.token_hex(32))

    async def create_account(self, username: str, password: str) -> Principal:
        hashed = await asyncio.to_thread(hash_password, username, password)
        async with self.sessions.begin() as session:
            account = Account(id=uuid4(), username=username, password_hash=hashed)
            session.add(account)
        return Principal(account.id, username)

    async def disable_account(self, username: str) -> bool:
        async with self.sessions.begin() as session:
            account = await repository.account_by_username(session, username)
            if account is None:
                return False
            account.enabled = False
            await session.execute(
                update(Session)
                .where(Session.account_id == account.id)
                .values(revoked_at=func.now())
            )
            return True

    async def login(self, username: str, password: str, *, peer: str) -> IssuedSession:
        validate_account(username, password)
        if not peer or len(peer) > 128:
            raise AuthenticationFailed("invalid login context")
        # Fail fast instead of allowing an unbounded password-hash queue.
        if self._hash_slots.locked():
            raise LoginThrottled("password verification capacity exhausted")
        async with self._hash_slots:
            async with self.sessions.begin() as session:
                now = await session.scalar(select(func.now()))
                assert now is not None
                await repository.prune_throttles(session, now)
                # Direct peer only; caller must never substitute forwarded client headers.
                bucket = hashlib.sha256(peer.encode("utf-8")).hexdigest()
                await repository.check_throttle_capacity(session, bucket)
                await repository.consume_login_budget(
                    session, bucket, now, now + timedelta(minutes=15)
                )
            async with self.sessions() as session:
                account = await repository.account_by_username(session, username)
                hashed = account.password_hash if account is not None else self._dummy_hash
            correct = await asyncio.to_thread(verify_password, hashed, password)
            if not correct or account is None or not account.enabled:
                raise AuthenticationFailed("invalid credentials")
            token, csrf = secrets.token_hex(32), secrets.token_hex(32)
            async with self.sessions.begin() as session:
                # Serialize with disabling the account and concurrent logins before issuing.
                account = await session.scalar(
                    select(Account).where(Account.id == account.id).with_for_update()
                )
                if account is None or not account.enabled:
                    raise AuthenticationFailed("invalid credentials")
                now = await session.scalar(select(func.now()))
                expires = now + timedelta(seconds=self.lifetime_seconds)
                await repository.prune_sessions(session, account.id)
                session.add(
                    Session(
                        token_hash=token_digest(token),
                        account_id=account.id,
                        csrf_hash=token_digest(csrf),
                        expires_at=expires,
                    )
                )
                principal = self._principal(account, token_digest(token))
            return IssuedSession(principal, expires, token, csrf)

    async def authenticate(self, token: str | None, *, csrf_token: str | None = None) -> Principal:
        if token is None or not TOKEN.fullmatch(token):
            raise AuthenticationFailed("invalid session")
        async with self.sessions() as session:
            row = await repository.current_session(session, token_digest(token))
            if row is None:
                raise AuthenticationFailed("invalid session")
            record, account = row
            if csrf_token is not None:
                if not TOKEN.fullmatch(csrf_token) or not hmac.compare_digest(
                    record.csrf_hash, token_digest(csrf_token)
                ):
                    raise AuthenticationFailed("invalid request token")
            return self._principal(account, record.token_hash)

    @staticmethod
    def _principal(account: Account, session_token_hash: str) -> Principal:
        return Principal(
            account.id,
            account.username,
            account.is_administrator,
            account.authorization_version,
            session_token_hash,
        )

    async def require_problem_maintenance(self, session, principal: Principal) -> None:
        if not session.in_transaction():
            raise ValueError("maintenance authorization requires an active write transaction")
        if (
            not principal.can_maintain_problems
            or principal.session_token_hash is None
            or not TOKEN.fullmatch(principal.session_token_hash)
        ):
            raise MaintenanceDenied("problem maintenance required")
        # Account first, then session: authority changes use that same lock order.
        # SHARE locks last until the caller commits/rolls back; a stale principal,
        # explicit logout, revocation or expired session cannot authorize a write.
        account = await repository.lock_authority(session, principal.account_id)
        if (
            account is None
            or not account.enabled
            or not account.is_administrator
            or account.authorization_version != principal.authorization_version
        ):
            raise MaintenanceDenied("problem maintenance required")
        row = await repository.current_session(
            session, principal.session_token_hash, lock_session=True
        )
        if row is None or row[1].id != account.id:
            raise MaintenanceDenied("problem maintenance required")

    async def logout(self, token: str, csrf_token: str) -> None:
        await self.authenticate(token, csrf_token=csrf_token)
        async with self.sessions.begin() as session:
            await repository.revoke(session, token_digest(token))


class ReservedMaintenance:
    """Import worker uses only the fixed, narrow SQL authority port."""

    async def require_reserved_maintenance(self, session, actor_id, epoch):
        if not session.in_transaction() or type(epoch) is not int or epoch < 1:
            raise MaintenanceDenied("invalid maintenance receipt")
        try:
            await session.execute(
                text("SELECT public.kf_lock_reserved_maintenance(:actor,:epoch)"),
                {"actor": actor_id, "epoch": epoch},
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "42501":
                raise MaintenanceDenied("problem maintenance revoked") from None
            raise
