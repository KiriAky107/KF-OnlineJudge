"""Uniform live session gates; domains still lock/recheck authority in write transactions."""

from typing import Annotated

from fastapi import Depends, HTTPException

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.modules.identity.public import Principal


async def maintenance_reader(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.can_maintain_problems:
        raise HTTPException(403, "problem maintenance required")
    return principal


async def maintenance_writer(
    principal: Annotated[Principal, Depends(unsafe_principal)],
) -> Principal:
    # unsafe_principal already checks an explicit Origin and the live session's
    # bound CSRF token. This capability check cannot replace the domain's port.
    if not principal.can_maintain_problems:
        raise HTTPException(403, "problem maintenance required")
    return principal
