"""Bounded, non-secret API responses for transient dependency/capacity failures."""

import errno

from asyncpg import CannotConnectNowError, PostgresConnectionError
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.responses import JSONResponse


async def temporarily_unavailable(_, __):
    # Write outcomes may be uncertain. Existing operation/submission UUIDs, not
    # automatic server retries, own recovery from a lost response.
    return JSONResponse(
        {"detail": "service temporarily unavailable"},
        status_code=503,
        headers={"Cache-Control": "no-store", "Retry-After": "5"},
    )


async def filesystem_failure(_, error: OSError):
    if error.errno in {errno.ENOSPC, errno.EDQUOT, errno.EMFILE, errno.ENFILE}:
        return await temporarily_unavailable(_, error)
    # Unrelated filesystem/programming failures retain server-error semantics.
    # Never serialize path/source/query/credential text from an exception.
    return JSONResponse(
        {"detail": "internal server error"}, status_code=500, headers={"Cache-Control": "no-store"}
    )


def install_handlers(app: FastAPI) -> None:
    for error in (
        ConnectionError,
        TimeoutError,
        OperationalError,
        PoolTimeoutError,
        PostgresConnectionError,
        CannotConnectNowError,
    ):
        app.add_exception_handler(error, temporarily_unavailable)
    app.add_exception_handler(OSError, filesystem_failure)
