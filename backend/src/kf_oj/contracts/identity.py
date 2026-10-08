from datetime import datetime
from typing import Annotated

from pydantic import Field, SecretStr

from kf_oj.contracts.judging import Digest, Identifier, WireModel


class LoginRequest(WireModel):
    username: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_]{3,24}$")]
    password: Annotated[SecretStr, Field(min_length=12, max_length=128)]


class CurrentUser(WireModel):
    id: Identifier
    username: str


class LoginResponse(WireModel):
    user: CurrentUser
    expires_at: datetime
    csrf_token: Digest = Field(repr=False)
