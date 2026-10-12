"""Read-only import projection for the ordinary operator CLI; no private archive data."""

from sqlalchemy import text

from kf_oj.modules.problems.import_models import ImportPool
from kf_oj.modules.problems.imports import ImportUnavailable, policy_digest


class ImportOperationQueries:
    def __init__(self, sessions, policy):
        self.sessions, self.policy = sessions, policy

    async def status(self) -> dict:
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout='1500'"))
            await session.execute(text("SET LOCAL lock_timeout='500'"))
            authorized = await session.scalar(
                text("""
                SELECT rolcanlogin AND NOT(rolsuper OR rolcreatedb OR rolcreaterole
                  OR rolreplication OR rolbypassrls)
                  AND pg_has_role(current_user,'kf_oj_problem_import','USAGE')
                  AND NOT pg_has_role(current_user,'kf_oj_platform','MEMBER')
                  AND NOT pg_has_role(current_user,'kf_oj_coordinator','MEMBER')
                  AND NOT pg_has_role(current_user,'kf_oj_identity_operator','MEMBER')
                  AND NOT pg_has_role(current_user,'kf_oj_identity_authority_owner','MEMBER')
                FROM pg_roles WHERE rolname=session_user
            """)
            )
            if authorized is not True:
                raise ImportUnavailable("dedicated import observation role required")
            pool = await session.get(ImportPool, 1)
            row = (
                (
                    await session.execute(
                        text("""
                SELECT clock_timestamp() now,
                  (SELECT count(*) FROM (SELECT 1 FROM problem_import_operations
                    WHERE state='uploaded' LIMIT 129) q) uploaded,
                  (SELECT count(*) FROM (SELECT 1 FROM problem_import_operations
                    WHERE state='validating' LIMIT 129) q) validating,
                  (SELECT coalesce(sum(reserved_bytes),0) FROM (
                    SELECT reserved_bytes FROM problem_import_operations
                    WHERE reserved_bytes>0 LIMIT 129) q) actual_reserved
            """)
                    )
                )
                .mappings()
                .one()
            )
            if pool is None or pool.reserved_bytes != row["actual_reserved"]:
                raise ImportUnavailable("import reservation consistency unknown")
            return {
                "schema_version": 1,
                "worker_ready": bool(
                    pool.worker_ready_until
                    and pool.worker_ready_until > row["now"]
                    and pool.worker_policy_digest == policy_digest(self.policy)
                ),
                "reserved_bytes": pool.reserved_bytes,
                "uploaded_capped": min(row["uploaded"], 128),
                "validating_capped": min(row["validating"], 128),
                "truncated": row["uploaded"] > 128 or row["validating"] > 128,
            }
