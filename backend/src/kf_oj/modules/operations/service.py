from uuid import uuid4

from sqlalchemy import func, select

from kf_oj.modules.operations.models import MaintenanceEvent
from kf_oj.modules.operations.public import Action


class MaintenanceAudit:
    async def record(
        self, session, actor_id, authorization_version, operation_id, action: Action, target_id=None
    ):
        if not session.in_transaction():
            raise ValueError("audit requires a write transaction")
        # Supplying the trusted DB timestamp avoids ORM INSERT ... RETURNING:
        # the ordinary importer has append-only authority and cannot SELECT this table.
        session.add(
            MaintenanceEvent(
                id=uuid4(),
                created_at=await session.scalar(select(func.clock_timestamp())),
                actor_id=actor_id,
                authorization_version=authorization_version,
                operation_id=operation_id,
                action=action,
                target_id=target_id,
            )
        )
