"""Ordinary fixed-query/authority models; no PostgreSQL or VM acceptance."""

import asyncio
import copy
import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
health = importlib.import_module("scripts.release.node_health")

# Original role policy, independent of the query builder and its result mapping.
GRANTS = {
    "api": {
        "accounts:SELECT": True,
        "evaluations:SELECT": True,
        "evaluations:state:UPDATE": False,
        "accounts:is_administrator:UPDATE": False,
    },
    "coordinator": {
        "evaluations:SELECT": True,
        "evaluations:state:UPDATE": True,
        "accounts:SELECT": False,
        "problems:active_version_id:UPDATE": False,
    },
    "import": {
        "problem_import_operations:SELECT": True,
        "problem_import_operations:state:UPDATE": True,
        "evaluations:state:UPDATE": False,
        "accounts:SELECT": False,
    },
    "identity": {
        "accounts:SELECT": False,
        "accounts:is_administrator:UPDATE": False,
        "accounts:password_hash:SELECT": False,
        "sessions:token_hash:SELECT": False,
        "evaluations:SELECT": False,
        "evaluations:state:UPDATE": False,
    },
}
FUNCTIONS = (
    "public.kf_manage_account_authority(text,text)",
    "public.kf_manage_account_authority_unlogged(text,text)",
)


def session(monkeypatch, mutate=None, failure=None):
    import asyncpg

    calls, closed, connections = [], [], []

    class Connection:
        def __init__(self, role):
            self.role = role

        async def fetchval(self, *_):
            raise AssertionError("one-row projection must not issue extra catalog statements")

        async def fetchrow(self, query, *parameters):
            role = self.role
            calls.append((role, query, parameters))
            assert query.count("FROM pg_roles WHERE rolname=session_user") == 1
            assert query.startswith(
                "SELECT session_user login,current_database() database,rolcanlogin canlogin,"
                "rolsuper superuser,rolcreatedb createdb,rolcreaterole createrole,"
                "rolreplication replication,rolbypassrls bypassrls,rolconnlimit connection_limit,"
                "pg_has_role(session_user,$1,'MEMBER') member,"
                "pg_has_role(session_user,$1,'USAGE') inherited,"
                "pg_has_role(session_user,$1,'SET') set_role,ARRAY["
            )
            assert not any(
                word in query.upper() for word in ("INSERT ", "UPDATE ", "DELETE ", "SET ROLE", ";")
            )
            assert parameters[0] == health.database.MEMBERSHIP[health.configuration.PROFILES[role]]
            for capability in ("MEMBER", "USAGE", "SET"):
                assert f"pg_has_role(session_user,$1,'{capability}')" in query
            expressions = re.findall(
                r"has_(table|column|function)_privilege\(current_user,((?:\$[0-9]+,?)+)(,'EXECUTE')?\)",
                query,
            )
            assert len(expressions) == len(GRANTS[role]) + len(FUNCTIONS)
            checked, functions, values, fvalues = [], [], [], []
            for kind, slots, execute in expressions:
                indices = [int(x[1:]) for x in slots.split(",")]
                assert all(1 < x <= len(parameters) for x in indices)
                args = tuple(parameters[x - 1] for x in indices)
                if kind == "function":
                    assert execute == ",'EXECUTE'"
                    assert len(args) == 1 and args[0] in FUNCTIONS
                    functions.append(args[0])
                    fvalues.append(role == "identity" and args[0] == FUNCTIONS[0])
                else:
                    assert execute == ""
                    assert len(args) == (2 if kind == "table" else 3)
                    key = ":".join(args)
                    assert key in GRANTS[role]
                    checked.append(key)
                    values.append(GRANTS[role][key])
            assert checked == list(GRANTS[role]) and functions == list(FUNCTIONS)
            # Every bind value is used; names/privilege tokens never enter SQL text.
            used = {int(x) for x in re.findall(r"\$([0-9]+)", query)}
            assert used == set(range(1, len(parameters) + 1))
            assert all("'" + str(value) + "'" not in query for value in parameters)
            if failure is not None:
                raise failure
            row = {
                "login": health.configuration.PROFILES[role],
                "database": "kf_onlinejudge",
                "canlogin": True,
                "superuser": False,
                "createdb": False,
                "createrole": False,
                "replication": False,
                "bypassrls": False,
                "connection_limit": 8,
                "member": True,
                "inherited": True,
                "set_role": False,
                "privileges": values,
                "functions": fvalues,
            }
            if mutate is not None:
                mutate(role, row)
            return row

        async def close(self, *, timeout):
            assert timeout == 1
            closed.append(self.role)

    async def connect(endpoint, **kwargs):
        role = endpoint.removeprefix("synthetic-local-")
        assert role in GRANTS
        assert kwargs == {
            "ssl": False,
            "timeout": 5,
            "command_timeout": 2,
            "server_settings": {
                "application_name": "kf-node-health",
                "default_transaction_read_only": "on",
                "statement_timeout": "2000",
                "lock_timeout": "500",
                "idle_in_transaction_session_timeout": "3000",
            },
        }
        connections.append(role)
        return Connection(role)

    monkeypatch.setattr(asyncpg, "connect", connect)
    return (
        {"database": {role: "synthetic-local-" + role for role in GRANTS}},
        calls,
        closed,
        connections,
    )


def test_all_original_authority_checks_use_one_readonly_statement_per_role(monkeypatch):
    endpoints, calls, closed, connected = session(monkeypatch)
    result = asyncio.run(health.roles(endpoints))
    assert result == {role: {"authority_verified": True} for role in GRANTS}
    assert len(calls) == 4 and [x[0] for x in calls] == list(GRANTS)
    assert closed == connected == list(GRANTS)


CONDITIONS = [
    (role, "privileges", index) for role, checks in GRANTS.items() for index in range(len(checks))
] + [(role, "functions", index) for role in GRANTS for index in range(2)]


@pytest.mark.parametrize("role,field,index", CONDITIONS)
def test_every_original_grant_and_function_mismatch_refuses(monkeypatch, role, field, index):
    def mutate(current, row):
        if current == role:
            row[field][index] = not row[field][index]

    endpoints, calls, closed, connected = session(monkeypatch, mutate)
    with pytest.raises(health.DeliveryError, match="database role authority differs"):
        asyncio.run(health.roles(endpoints))
    assert calls[-1][0] == role and closed == connected


@pytest.mark.parametrize(
    "field,value",
    [
        ("privileges", None),
        ("privileges", {}),
        ("privileges", []),
        ("privileges", [True] * 5),
        ("privileges", [True, True, 0, False]),
        ("functions", [False]),
        ("functions", [False, None]),
        ("functions", [False, 0]),
    ],
)
def test_invalid_projection_shapes_and_non_boolean_values_refuse(monkeypatch, field, value):
    def mutate(_, row):
        row[field] = copy.deepcopy(value)

    endpoints, calls, closed, connected = session(monkeypatch, mutate)
    with pytest.raises(health.DeliveryError, match="database role projection values differ"):
        asyncio.run(health.roles(endpoints))
    assert len(calls) == 1 and closed == connected == ["api"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("superuser", True),
        ("createdb", True),
        ("createrole", True),
        ("replication", True),
        ("bypassrls", True),
        ("member", False),
        ("inherited", False),
        ("set_role", True),
        ("connection_limit", 9),
        ("database", "other_database"),
        ("canlogin", 1),
    ],
)
def test_role_flags_and_identity_still_refuse(monkeypatch, field, value):
    def mutate(_, row):
        row[field] = value

    endpoints, calls, closed, connected = session(monkeypatch, mutate)
    with pytest.raises(health.DeliveryError, match="database role authority differs"):
        asyncio.run(health.roles(endpoints))
    assert len(calls) == 1 and closed == connected == ["api"]


def test_catalog_failure_preserves_error_and_closes_connection(monkeypatch):
    original = RuntimeError("synthetic catalog failure")
    endpoints, calls, closed, connected = session(monkeypatch, failure=original)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(health.roles(endpoints))
    assert caught.value is original and len(calls) == 1 and closed == connected == ["api"]
