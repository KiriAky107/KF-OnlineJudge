"""Explicit DB identities only; all application schema changes belong to Alembic."""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .configuration import PROFILES, connections, private
from .policy import DeliveryError, fields, read_json

GROUPS = (
    "kf_oj_platform",
    "kf_oj_coordinator",
    "kf_oj_problem_import",
    "kf_oj_identity_operator",
    "kf_oj_identity_authority_owner",
)
MEMBERSHIP = {
    "kf_platform": "kf_oj_platform",
    "kf_coordinator": "kf_oj_coordinator",
    "kf_problem_import": "kf_oj_problem_import",
    "kf_identity_operator": "kf_oj_identity_operator",
    "kf_migrator": "kf_oj_identity_authority_owner",
}
DATABASE = "kf_onlinejudge"
SETTINGS = {
    "statement_timeout": "5000",
    "lock_timeout": "1000",
    "idle_in_transaction_session_timeout": "5000",
    "application_name": "kf-delivery-identities",
}


def operator_url(path: Path) -> str:
    private(path)
    value = fields(read_json(path, 4096), {"schema_version", "url"}, "DB operator connection")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DeliveryError("unsupported DB operator input")
    raw = value["url"]
    if not isinstance(raw, str) or len(raw) > 1024:
        raise DeliveryError("invalid DB operator connection")
    parts = urlsplit(raw)
    if (
        parts.scheme != "postgresql"
        or parts.hostname != "127.0.0.1"
        or parts.port != 5432
        or parts.username != "postgres"
        or parts.path != "/postgres"
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", parts.password or "")
    ):
        raise DeliveryError("explicit local DB operator identity required")
    return raw


async def _provision(operator: Path, endpoint_path: Path) -> dict:
    import asyncpg

    endpoints = connections(endpoint_path)
    db = await asyncpg.connect(
        operator_url(operator),
        timeout=3,
        command_timeout=5,
        server_settings={**SETTINGS, "log_min_error_statement": "panic"},
    )
    try:
        if not await db.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"):
            raise DeliveryError("explicit local DB identity operator required")
        for name, expected in (
            ("log_statement", "none"),
            ("log_min_duration_statement", "-1"),
            ("log_min_duration_sample", "-1"),
            ("log_transaction_sample_rate", "0"),
        ):
            if await db.fetchval("SHOW " + name) != expected:
                raise DeliveryError("DB statement logging incompatible with secret provisioning")
        async with db.transaction():
            for name in (*GROUPS, *PROFILES.values()):
                login = name in PROFILES.values()
                row = await db.fetchrow(
                    "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,"
                    "rolreplication,rolbypassrls,"
                    "rolconnlimit FROM pg_roles WHERE rolname=$1",
                    name,
                )
                wanted = (login, False, False, False, False, False, 8 if login else -1)
                if row is None:
                    # Names are fixed public constants. The password alphabet
                    # is validated before any utility SQL is constructed.
                    statement = (
                        "CREATE ROLE "
                        + name
                        + (" LOGIN" if login else " NOLOGIN")
                        + " NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                        "NOBYPASSRLS INHERIT"
                    )
                    if login:
                        statement += " CONNECTION LIMIT 8"
                        key = next(key for key, role in PROFILES.items() if role == name)
                        password = urlsplit(endpoints["database"][key]).password
                        statement += " PASSWORD '" + password + "'"
                    await db.execute(statement)
                elif tuple(row) != wanted:
                    raise DeliveryError("existing DB authority differs; refused overwrite")
                memberships = [
                    tuple(row)
                    for row in await db.fetch(
                        "SELECT r.rolname,m.admin_option,m.inherit_option,m.set_option "
                        "FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
                        "JOIN pg_roles u ON u.oid=m.member WHERE u.rolname=$1",
                        name,
                    )
                ]
                expected = (
                    [] if not login else [(MEMBERSHIP[name], False, True, name == "kf_migrator")]
                )
                if memberships and memberships != expected:
                    raise DeliveryError("existing DB role membership differs")
            for login, group in MEMBERSHIP.items():
                set_role = "TRUE" if login == "kf_migrator" else "FALSE"
                for option, value in (("ADMIN", "FALSE"), ("INHERIT", "TRUE"), ("SET", set_role)):
                    # Each command uses the documented single membership-option
                    # form; all three changes are in the same transaction.
                    await db.execute(f"GRANT {group} TO {login} WITH {option} {value}")
        existing = await db.fetchval(
            "SELECT r.rolname FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba "
            "WHERE d.datname=$1",
            DATABASE,
        )
        if existing is None:
            await db.execute(
                "CREATE DATABASE kf_onlinejudge OWNER kf_migrator "
                "TEMPLATE template0 ENCODING 'UTF8'"
            )
        elif existing != "kf_migrator":
            raise DeliveryError("existing application database owner differs")
    finally:
        await db.close(timeout=3)
    verified = []
    for name, raw in endpoints["database"].items():
        session = await asyncpg.connect(raw, timeout=3, command_timeout=5, server_settings=SETTINGS)
        try:
            row = await session.fetchrow("SELECT current_user,current_database()")
            if tuple(row) != (PROFILES[name], DATABASE):
                raise DeliveryError("DB identity readback differs")
            verified.append(name)
        finally:
            await session.close(timeout=3)
    return {
        "database_identities_provisioned": sorted(verified),
        "application_schema_migrated": False,
        "application_accounts_created": False,
        "secrets_reset": False,
    }


def provision(operator: Path, endpoint_path: Path) -> dict:
    """No table DDL, no secret reset and no automatic application initialization.

    Pre-created NOLOGIN groups allow the unchanged Backend Alembic migrations
    to run under a separate non-superuser database owner. Only this migration
    login inherits and can SET the narrow authority-function owner, allowing
    unchanged Alembic to grant/rename its already owned functions without global
    superuser/create-role privileges. Ordinary logins inherit their
    own one group and cannot grant it. Unexpected existing authority is refused.
    """
    if os.geteuid() != 0:
        raise DeliveryError("DB provisioning requires the root operator")
    import asyncpg

    try:
        return asyncio.run(asyncio.wait_for(_provision(operator, endpoint_path), timeout=120))
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as error:
        # A server error may include utility SQL; never format it or its query.
        raise DeliveryError("DB identity provisioning/readback refused") from error
