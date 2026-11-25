"""Only verified principals cross business module boundaries."""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    account_id: UUID
    username: str
    can_maintain_problems: bool = False
    authorization_version: int = 1
    session_token_hash: str | None = field(default=None, repr=False, compare=False)


class MaintenanceDenied(ValueError):
    pass


class ProblemMaintenancePort(Protocol):
    async def require_problem_maintenance(self, session, principal: Principal) -> None:
        """Recheck and lock live authority/session inside the caller's write transaction."""
        ...


class AuthenticationFailed(ValueError):
    pass


class ReservedMaintenancePort(Protocol):
    async def require_reserved_maintenance(self, session, actor_id: UUID, epoch: int) -> None:
        """Recheck/lock an immutable accepted receipt, with no login/session secret access."""
        ...


class LoginThrottled(ValueError):
    pass


class ActiveSessionPort(Protocol):
    async def require_active_session(self, session, principal: Principal) -> bool:
        """Verify the live session and return whether its account is an administrator."""
        ...


@dataclass(frozen=True, slots=True)
class AcceptedSession:
    """A job proof derived from a live session. It cannot authenticate a browser."""

    authorization_version: int
    session_proof: str = field(repr=False)

    @classmethod
    def from_principal(cls, principal: Principal) -> "AcceptedSession":
        if principal.session_token_hash is None:
            raise AuthenticationFailed("session required to start a learning run")
        proof = sha256(principal.session_token_hash.encode("ascii")).hexdigest()
        return cls(principal.authorization_version, proof)


def accepted_session_access(owner_id: UUID, receipt: AcceptedSession) -> ActiveSessionPort:
    from kf_oj.modules.identity.service import AcceptedSessionAccess

    return AcceptedSessionAccess(owner_id, receipt)


class AdministrationDenied(ValueError):
    pass


class AdministrationPort(ActiveSessionPort, Protocol):
    async def require_administrator(self, session, principal: Principal) -> None:
        """Recheck and lock the administrator account and login session."""
        ...


class ContestAdministrationPort(AdministrationPort, Protocol):
    async def contest_account_ids(self, session, usernames: tuple[str, ...]) -> dict[str, UUID]: ...
