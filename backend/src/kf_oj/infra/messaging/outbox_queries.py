"""Read-only bounded outbox counts, without broker or delivery dependencies."""

from sqlalchemy import text


class OutboxQueries:
    def __init__(self, sessions):
        self.sessions = sessions

    async def status(self):
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout='1500'"))
            await session.execute(text("SET LOCAL lock_timeout='500'"))
            safe = await session.scalar(
                text("""
                SELECT rolcanlogin AND NOT(rolsuper OR rolcreatedb OR rolcreaterole
                  OR rolreplication OR rolbypassrls)
                  AND pg_has_role(current_user,'kf_oj_platform','USAGE')
                  AND NOT pg_has_role(current_user,'kf_oj_coordinator','MEMBER')
                  AND NOT pg_has_role(current_user,'kf_oj_identity_authority_owner','MEMBER')
                  AND NOT has_column_privilege(current_user,'evaluations','state','UPDATE')
                FROM pg_roles WHERE rolname=session_user
            """)
            )
            if safe is not True:
                raise ValueError("dedicated outbox projection role required")
            row = (
                (
                    await session.execute(
                        text("""
                SELECT
                  (SELECT count(*) FROM (SELECT 1 FROM outbox
                    WHERE delivered_at IS NULL LIMIT 129) p) pending,
                  (SELECT count(*) FROM (SELECT 1 FROM outbox WHERE delivered_at IS NULL
                    AND locked_until>clock_timestamp() LIMIT 129) l) locked
            """)
                    )
                )
                .mappings()
                .one()
            )
            return {
                "schema_version": 1,
                "pending_capped": min(row["pending"], 128),
                "locked_capped": min(row["locked"], 128),
                "truncated": row["pending"] > 128 or row["locked"] > 128,
            }
