"""Only verified principals cross business module boundaries."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    account_id: UUID
    username: str


class AuthenticationFailed(ValueError):
    pass


class LoginThrottled(ValueError):
    pass
