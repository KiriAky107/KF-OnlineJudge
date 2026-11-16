"""Trusted notification and startup models; actual managed readiness remains VM-only."""

import asyncio
import importlib
import logging
import os
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from kf_oj.infra import service_readiness
from kf_oj.infra.messaging import publisher
from kf_oj.workers import problem_import

bootstrap = importlib.import_module("scripts.release.service_bootstrap")


def test_fixed_ready_datagram_and_no_inherited_notify_socket(monkeypatch):
    with TemporaryDirectory(prefix="kf-ready-") as directory:
        address = str(Path(directory) / "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(address)
            receiver.settimeout(1)
            monkeypatch.setenv("NOTIFY_SOCKET", address)
            assert service_readiness.notify_ready() is True
            assert receiver.recv(64) == b"READY=1"
            assert service_readiness.notify_ready() is False


@pytest.mark.parametrize("address", ["", "@", "relative", "/bad\npath", "/" + "x" * 108])
def test_invalid_notification_is_consumed_without_exposing_address(monkeypatch, address):
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    with pytest.raises(ValueError, match="^invalid service notification socket$"):
        service_readiness.notify_ready()
    assert service_readiness.notify_ready() is False


@pytest.mark.parametrize("role", ["outbox", "problem-import", "api"])
def test_bootstrap_forwards_only_worker_notification_socket(monkeypatch, role):
    for key in list(bootstrap.os.environ):
        if key.startswith("KF_OJ_"):
            monkeypatch.delenv(key)
    for key in bootstrap.ROLE_ENVIRONMENT[role]:
        monkeypatch.setenv(key, "ordinary-model")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-forwarded")
    monkeypatch.setattr(bootstrap.preflight, "protected", lambda *_: None)
    _, environment = bootstrap.python_command(role, "sd-" + "a" * 12)
    assert "UNRELATED_SECRET" not in environment
    if role == "api":
        assert "NOTIFY_SOCKET" not in environment
    else:
        assert environment["NOTIFY_SOCKET"] == "/run/systemd/notify"
        monkeypatch.delenv("NOTIFY_SOCKET")
        with pytest.raises(bootstrap.DeliveryError, match="notification socket required"):
            bootstrap.python_command(role, "sd-" + "a" * 12)


@pytest.mark.parametrize("authorized", [True, False])
def test_outbox_notifies_only_after_database_authority_and_context_close(monkeypatch, authorized):
    trace = []
    stop = asyncio.Event()

    class Engine:
        def connect(self):
            return self

        async def __aenter__(self):
            trace.append("connected")
            return self

        async def scalar(self, _):
            trace.append("authority")
            return authorized

        async def __aexit__(self, *_):
            trace.append("connection-closed")

        async def dispose(self):
            trace.append("disposed")

    def ready():
        trace.append("ready")
        stop.set()

    for name in ("aio_pika", "aiormq", "pamqp"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "disabled", logger.disabled)
        monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(publisher, "build_engine", lambda _: Engine())
    monkeypatch.setattr(publisher, "notify_ready", ready)
    settings = SimpleNamespace(amqp_url=SimpleNamespace(get_secret_value=lambda: "unused"))
    if authorized:
        asyncio.run(publisher.run(settings, stop))
        assert trace == [
            "connected",
            "authority",
            "connection-closed",
            "ready",
            "disposed",
        ]
    else:
        with pytest.raises(ValueError, match="dedicated platform database role required"):
            asyncio.run(publisher.run(settings, stop))
        assert trace == ["connected", "authority", "connection-closed", "disposed"]


@pytest.mark.parametrize("heartbeat_failure", [False, True])
def test_import_initial_heartbeat_precedes_notify_and_any_uploaded_work(
    monkeypatch, heartbeat_failure
):
    trace = []
    stop = asyncio.Event()

    class Store:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            trace.append("closed")

    class Engine:
        async def dispose(self):
            trace.append("disposed")

    class Worker:
        def __init__(self, *_):
            pass

        async def heartbeat(self):
            trace.append("heartbeat-committed")
            if heartbeat_failure:
                raise ValueError("ordinary failed authority model")

        async def once(self):
            raise AssertionError("uploaded work must not run before startup readiness")

    def ready():
        trace.append("ready")
        stop.set()

    settings = SimpleNamespace(
        import_staging_root="unused",
        artifact_root="unused",
        judge_admission=object(),
        import_worker_cgroup="unused",
        artifact_max_bytes=1,
        artifact_max_files=1,
    )
    monkeypatch.setattr(problem_import, "Settings", lambda: settings)
    monkeypatch.setattr(problem_import.asyncio, "Event", lambda: stop)
    monkeypatch.setattr(problem_import, "verify_worker_limits", lambda _: None)
    monkeypatch.setattr(problem_import, "build_engine", lambda _: Engine())
    monkeypatch.setattr(problem_import, "build_session_factory", lambda _: object())
    monkeypatch.setattr(problem_import, "ArtifactStore", Store)
    monkeypatch.setattr(problem_import, "ImportStaging", Store)
    monkeypatch.setattr(problem_import, "ArtifactRepository", lambda _: object())
    monkeypatch.setattr(problem_import, "ImportWorker", Worker)
    monkeypatch.setattr(problem_import, "notify_ready", ready)
    if heartbeat_failure:
        with pytest.raises(ValueError, match="ordinary failed authority model"):
            asyncio.run(problem_import.run())
        assert trace == ["heartbeat-committed", "closed", "closed", "disposed"]
    else:
        asyncio.run(problem_import.run())
        assert trace == ["heartbeat-committed", "ready", "closed", "closed", "disposed"]


@pytest.mark.parametrize(
    "helper_name,role,status_module",
    [
        ("outbox_health", "outbox", "kf_oj.workers.outbox_status"),
        ("import_health", "problem-import", "kf_oj.workers.problem_status"),
    ],
)
@pytest.mark.parametrize("notify", [None, "relative-invalid-notification", "/run/systemd/notify"])
def test_readonly_health_exec_without_startup_notification(
    monkeypatch, helper_name, role, status_module, notify
):
    helper = importlib.import_module("scripts.release." + helper_name)
    for key in list(os.environ):
        if key.startswith("KF_OJ_"):
            monkeypatch.delenv(key)
    for key in bootstrap.ROLE_ENVIRONMENT[role]:
        monkeypatch.setenv(key, "trusted-model")
    if notify is None:
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    else:
        monkeypatch.setenv("NOTIFY_SOCKET", notify)
    monkeypatch.setenv("UNRELATED_SECRET", "excluded-trusted-model")
    monkeypatch.setattr(bootstrap.preflight, "protected", lambda *_: None)
    release = "sd-" + "a" * 12
    payload = Path("/opt/kf-onlinejudge/releases") / release / "payload"
    monkeypatch.setattr(
        helper,
        "__file__",
        str(payload / "delivery/scripts/release" / (helper_name + ".py")),
    )
    monkeypatch.setattr(sys, "argv", [helper_name, "--inside"])
    trace = []
    monkeypatch.setattr(
        helper.operator_units, "guarded", lambda name: trace.append(("guard", name))
    )
    original_read = Path.read_text

    def private_profile(path, *args, **kwargs):
        if path == Path("/proc/self/status"):
            return "NSpid:\t123 1\n"
        if path == Path("/proc/net/dev"):
            return "header\nheader\n lo: 0\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", private_profile)

    class FixedExecReached(Exception):
        pass

    def fixed_exec(program, command, environment):
        assert trace == [("guard", helper_name.replace("_", "-"))]
        assert program == str(payload / "python/bin/python3.13")
        assert command == [program, "-s", "-P", "-m", status_module]
        assert "NOTIFY_SOCKET" not in environment and "UNRELATED_SECRET" not in environment
        assert {k for k in environment if k.startswith("KF_OJ_")} == bootstrap.ROLE_ENVIRONMENT[
            role
        ]
        raise FixedExecReached

    monkeypatch.setattr(os, "execve", fixed_exec)
    with pytest.raises(FixedExecReached):
        helper.main()


def test_python_status_command_rejects_service_without_fixed_read_projection():
    with pytest.raises(bootstrap.DeliveryError, match="unknown ordinary status role"):
        bootstrap.python_status_command("api", "sd-" + "a" * 12)
