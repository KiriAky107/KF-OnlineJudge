from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, SecretStr

from kf_oj.contracts.judging import Digest, Identifier, WireModel


class LoginRequest(WireModel):
    username: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_]{3,24}$")]
    password: Annotated[SecretStr, Field(min_length=12, max_length=128)]


class AccountCapabilities(WireModel):
    schema_version: Literal[1] = 1
    problem_maintenance: bool = False
    authorization_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")] = "1"


class CurrentUser(WireModel):
    id: Identifier
    username: str
    capabilities: AccountCapabilities = Field(default_factory=AccountCapabilities)


class LoginResponse(WireModel):
    user: CurrentUser
    expires_at: datetime
    csrf_token: Digest = Field(repr=False)
