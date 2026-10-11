"""Only authorized administrators read bounded append-only maintenance projections."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, tuple_

from kf_oj.contracts.problem_maintenance import MaintenanceAuditEvent, MaintenanceAuditPage
from kf_oj.modules.identity.public import ProblemMaintenancePort
from kf_oj.modules.operations.models import MaintenanceEvent


class MaintenanceAuditQueries:
    def __init__(self, sessions, identity: ProblemMaintenancePort):
        self.sessions, self.identity = sessions, identity

    async def page(self, principal, *, before=None, limit=20, action=None):
        if not 1 <= limit <= 100:
            raise ValueError("invalid page limit")
        cursor = None
        if before is not None:
            stamp, identifier = before.split("|")
            stamp = datetime.fromisoformat(stamp)
            if stamp.tzinfo is None:
                raise ValueError("invalid audit cursor")
            cursor = stamp, UUID(identifier)
        async with self.sessions.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout='5s'"))
            await session.execute(text("SET LOCAL lock_timeout='2s'"))
            await self.identity.require_problem_maintenance(session, principal)
            q = select(MaintenanceEvent)
            if cursor:
                q = q.where(tuple_(MaintenanceEvent.created_at, MaintenanceEvent.id) < cursor)
            if action:
                q = q.where(MaintenanceEvent.action == action)
            rows = list(
                await session.scalars(
                    q.order_by(
                        MaintenanceEvent.created_at.desc(), MaintenanceEvent.id.desc()
                    ).limit(limit + 1)
                )
            )
            visible = rows[:limit]
            return MaintenanceAuditPage(
                items=tuple(
                    MaintenanceAuditEvent(
                        id=r.id,
                        created_at=r.created_at,
                        actor_kind=r.actor_kind,
                        actor_id=r.actor_id,
                        operator_name=r.operator_name,
                        authorization_version=str(r.authorization_version),
                        operation_id=r.operation_id,
                        action=r.action,
                        target_id=r.target_id,
                    )
                    for r in visible
                ),
                next_before=f"{visible[-1].created_at.isoformat()}|{visible[-1].id}"
                if len(rows) > limit
                else None,
            )
