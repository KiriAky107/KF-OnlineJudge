"""Only verified principals cross business module boundaries."""

from dataclasses import dataclass, field
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
