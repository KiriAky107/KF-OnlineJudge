"""Ordinary trusted exception models; no database, broker or student execution."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from kf_oj.infra.messaging import publisher
from kf_oj.infra.messaging import publisher_diagnostics as diagnostic


def test_fatal_startup_reads_actual_phase_without_credentials(monkeypatch):
    secret = "postgresql://private_identity:synthetic_password@private.invalid/database"

    class Engine:
        def connect(self):
            return self

        async def __aenter__(self):
            raise TimeoutError(secret)

        async def __aexit__(self, *args):
            raise AssertionError("not entered")

        async def dispose(self):
            pass

    for name in ("aio_pika", "aiormq", "pamqp"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "disabled", logger.disabled)
        monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(publisher, "build_engine", lambda _: Engine())
    settings = SimpleNamespace(amqp_url=SimpleNamespace(get_secret_value=lambda: secret))
    try:
        asyncio.run(publisher.run(settings, asyncio.Event()))
    except TimeoutError as error:
        record = diagnostic.failure_record(error, publisher.run.__code__)
    else:
        raise AssertionError("original startup failure was swallowed")
    assert record["phase"] == "database_connect"
    assert record["errors"][0]["type"] == "TimeoutError"
    assert any(frame["file"] == "publisher.py" for frame in record["errors"][0]["frames"])
    assert "synthetic_password" not in json.dumps(record)
    assert "private_identity" not in json.dumps(record)
    assert "private.invalid" not in json.dumps(record)


@pytest.mark.parametrize("state", [True, 12345, {}, "password", "１２３４５", "08_06"])
def test_non_sqlstate_values_are_never_forwarded(state):
    error = ValueError("synthetic_secret")
    error.sqlstate = state
    assert diagnostic.failure_record(error, publisher.run.__code__)["errors"][0]["sqlstate"] is None


def test_only_basename_not_private_frame_path_is_forwarded():
    namespace = {}
    exec(
        compile(
            "def run():\n raise ValueError('synthetic_secret_message')",
            "/private_secret/publisher.py",
            "exec",
        ),
        namespace,
    )
    try:
        namespace["run"]()
    except ValueError as error:
        value = diagnostic.failure_record(error, publisher.run.__code__)
    else:
        raise AssertionError("ordinary failure missing")
    assert value["errors"][0]["frames"][-1]["file"] == "publisher.py"
    assert "private_secret" not in json.dumps(value)
    assert "synthetic_secret_message" not in json.dumps(value)


def test_unknown_error_name_and_fields_are_not_forwarded(capsys):
    error = type("synthetic_secret_class", (Exception,), {})("synthetic_secret_message")
    error.sqlstate = "synthetic_secret_state"
    error.password = "synthetic_secret_field"
    diagnostic.emit_failure(error, publisher.run.__code__)
    line = capsys.readouterr().err
    assert line.startswith(diagnostic.PREFIX) and len(line) < 3200
    assert "synthetic_secret" not in line
    record = json.loads(line.removeprefix(diagnostic.PREFIX))
    assert record["phase"] == "unknown"
    assert record["errors"] == [
        {
            "relation": "error",
            "type": "other",
            "sqlstate": None,
            "frames": [],
            "frames_truncated": False,
        }
    ]


def test_exception_graph_is_bounded_and_sqlstate_is_typed():
    error = ValueError("secret root")
    cursor = error
    for _ in range(12):
        child = OSError("secret child")
        child.sqlstate = "08006"
        cursor.__cause__ = child
        cursor.orig = child
        cursor = child
    cursor.__context__ = error
    value = diagnostic.failure_record(error, publisher.run.__code__)
    assert len(value["errors"]) == 4 and value["chain_truncated"] is True
    assert value["errors"][1]["sqlstate"] == "08006"
    assert "secret" not in json.dumps(value)


def test_diagnostic_failure_retains_original_nonzero_exit(monkeypatch, capsys):
    def failed_entry(coroutine):
        coroutine.close()
        raise ValueError("synthetic_secret")

    def failed_diagnostic(*_):
        raise RuntimeError("synthetic_other_secret")

    monkeypatch.setattr(publisher.asyncio, "run", failed_entry)
    monkeypatch.setattr(publisher, "emit_failure", failed_diagnostic)
    try:
        publisher.main()
    except SystemExit as error:
        assert error.code == "outbox unavailable"
    else:
        raise AssertionError("original nonzero failure was replaced")
    assert capsys.readouterr().err == ""
