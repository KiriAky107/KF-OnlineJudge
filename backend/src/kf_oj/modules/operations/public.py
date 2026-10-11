"""Accepted actor receipts and finite maintenance events; no private data/log text."""

from typing import Literal, Protocol
from uuid import UUID

Action = Literal[
    "upload_created",
    "upload_received",
    "import_ready",
    "import_failed",
    "import_discarded",
    "import_expired",
    "published",
    "disabled",
    "restored",
]


class MaintenanceAuditPort(Protocol):
    async def record(
        self,
        session,
        actor_id: UUID,
        authorization_version: int,
        operation_id: UUID,
        action: Action,
        target_id: UUID | None = None,
    ) -> None: ...
