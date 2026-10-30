"""Bounded outbox failure locations; never serialize errors or connection inputs."""

import json
import re
import sys
from pathlib import PurePath
from types import CodeType

PREFIX = "KF_OUTBOX_START_FAILURE "
PHASES = {"configuration", "database_connect", "database_authority", "running"}
ERROR_TYPES = {
    "ValueError",
    "ValidationError",
    "TimeoutError",
    "OSError",
    "PermissionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "MemoryError",
    "RuntimeError",
    "InvalidPasswordError",
    "InvalidAuthorizationSpecificationError",
    "CannotConnectNowError",
    "InterfaceError",
    "OperationalError",
    "DBAPIError",
    "ProgrammingError",
    "DataError",
    "InternalServerError",
    "PostgresError",
    "InvalidCatalogNameError",
    "InsufficientPrivilegeError",
    "TooManyConnectionsError",
    "QueryCanceledError",
    "ConnectionDoesNotExistError",
    "InternalClientError",
    "ReadOnlySQLTransactionError",
}
FILES = {
    "publisher.py",
    "session.py",
    "settings.py",
    "base_events.py",
    "runners.py",
    "tasks.py",
    "engine.py",
    "base.py",
    "default.py",
    "connection.py",
    "transaction.py",
    "connect_utils.py",
    "asyncpg.py",
    "_concurrency_py3k.py",
    "impl.py",
    "selector_events.py",
    "main.py",
}
FUNCTIONS = {
    "main",
    "entry",
    "run",
    "build_engine",
    "connect",
    "execute",
    "scalar",
    "_execute",
    "__execute",
    "_do_execute",
    "_execute_context",
    "_exec_single_context",
    "_handle_dbapi_exception",
    "_wrap_pool_connect",
    "raw_connection",
    "run_until_complete",
    "wait_for",
    "__aenter__",
    "__aexit__",
    "_connect",
    "_connect_addr",
    "__connect_addr",
    "_get_pool_impl",
    "_create_connection",
    "_do_get",
    "_invoke_creator",
    "await_only",
    "greenlet_spawn",
    "_connection_for_bind",
    "start",
    "_async_ping",
    "_do_ping_w_event",
    "__init__",
    "_sock_connect_cb",
    "sock_connect",
    "_ensure_connected",
    "_get_statement",
    "_execute_internal",
    "close",
    "dispose",
    "_cleanup_stmts",
}


def failure_record(error: Exception, run_code: CodeType) -> dict:
    """Read existing exception/frame metadata, without altering the failed operation."""
    pending: list[tuple[str, BaseException]] = [("error", error)]
    seen = set()
    records = []
    phase = "unknown"
    while pending and len(records) < 4:
        relation, item = pending.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        kind = type(item).__name__
        state = getattr(item, "sqlstate", None)
        if type(state) is not str or re.fullmatch(r"[A-Z0-9]{5}", state) is None:
            state = None
        frames = []
        trace = item.__traceback__
        depth = 0
        while trace is not None and depth < 32:
            code = trace.tb_frame.f_code
            if code is run_code:
                actual = trace.tb_frame.f_locals.get("_startup_phase")
                if type(actual) is str and actual in PHASES:
                    phase = actual
            name = PurePath(code.co_filename).name
            if name in FILES and code.co_name in FUNCTIONS and 1 <= trace.tb_lineno <= 100000:
                frames.append({"file": name, "line": trace.tb_lineno, "function": code.co_name})
            trace = trace.tb_next
            depth += 1
        records.append(
            {
                "relation": relation,
                "type": kind if kind in ERROR_TYPES else "other",
                "sqlstate": state,
                "frames": frames[-4:],
                "frames_truncated": len(frames) > 4 or trace is not None,
            }
        )
        for link in ("orig", "__cause__", "__context__"):
            child = getattr(item, link, None)
            if isinstance(child, BaseException) and id(child) not in seen:
                pending.append(
                    ({"orig": "orig", "__cause__": "cause", "__context__": "context"}[link], child)
                )
    return {
        "schema_version": 1,
        "component": "outbox",
        "phase": phase,
        "errors": records,
        "chain_truncated": any(id(item) not in seen for _, item in pending),
    }


def emit_failure(error: Exception, run_code: CodeType) -> None:
    encoded = json.dumps(failure_record(error, run_code), separators=(",", ":"), ensure_ascii=True)
    if len(encoded) > 3072:
        raise ValueError("outbox diagnostic bound exceeded")
    print(PREFIX + encoded, file=sys.stderr, flush=True)
