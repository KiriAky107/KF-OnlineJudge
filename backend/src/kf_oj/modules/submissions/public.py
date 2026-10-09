from typing import Protocol
from uuid import UUID


class CancelUnavailable(ValueError):
    pass


class CancellationPort(Protocol):
    async def cancel(self, evaluation_id: UUID) -> bool: ...


class SubmissionUnavailable(ValueError):
    pass


class WorkerAccessDenied(ValueError):
    """Never reveal whether the evaluation, attempt or artifact exists."""


class WorkerReadBusy(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class SubmissionBusy(ValueError):
    pass
