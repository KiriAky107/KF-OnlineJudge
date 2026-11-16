"""Ordinary controller/codec models; no service or isolation acceptance."""

import copy
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
units = importlib.import_module("scripts.release.operator_units")
install = importlib.import_module("scripts.release.install")
service_install = importlib.import_module("scripts.release.service_install")


def record():
    return units.failure_record(
        "factory-health",
        "exit",
        subprocess.CompletedProcess([], 1, b"private-output", b"private-error"),
        units.DeliveryError("operator helper unavailable"),
    )


@pytest.fixture
def dispatched(monkeypatch):
    selected = {"release_id": "sd-" + "a" * 12, "configuration_id": "cfg-" + "b" * 24}
    payload = install.RELEASES / selected["release_id"] / "payload"
    monkeypatch.setattr(units.os, "getuid", lambda: 0)
    monkeypatch.setattr(units.os, "geteuid", lambda: 0)
    monkeypatch.setattr(install, "selection", lambda **_: selected)
    calls = []

    def manager(*args):
        calls.append(args)
        return "LoadState=not-found\nActiveState=inactive\nMainPID=0\nControlGroup=\n"

    monkeypatch.setattr(service_install, "systemctl", manager)
    return payload, calls


@pytest.mark.parametrize("failure", ["exit", "timeout", "decode", "stderr_bound"])
def test_dispatch_diagnostics_keep_limits_cleanup_and_only_output_digests(
    monkeypatch, dispatched, failure
):
    payload, calls = dispatched

    def execute(command, **kwargs):
        assert kwargs["timeout"] == 915
        assert "--property=MemoryMax=256M" in command
        assert "--property=RuntimeMaxSec=900" in command
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                command, 915, output=b"private-output", stderr=b"private-error"
            )
        return subprocess.CompletedProcess(
            command,
            1 if failure == "exit" else 0,
            b"private-output",
            b"private-error" * (400 if failure == "stderr_bound" else 1),
        )

    monkeypatch.setattr(units.subprocess, "run", execute)
    with pytest.raises(units.OperatorExecutionError) as caught:
        units.run("node-maintenance", payload, action="start")
    error = caught.value
    assert len(calls) == 2
    row = error.failures[0]
    assert row["helper"] == "node-maintenance"
    assert row["outcome"] == ("exit" if failure == "stderr_bound" else failure)
    assert row["stdout_bytes"] == 14
    assert row["stdout_sha256"] == hashlib.sha256(b"private-output").hexdigest()
    text = units.diagnostic_suffix(error)
    assert "private-" not in text and len(text) <= 3072
    assert units.diagnostic_chain(text.encode()) == error.failures


def test_nested_failure_retains_inner_helper_and_outer_actual_exit(monkeypatch, dispatched):
    payload, calls = dispatched
    child = units.OperatorExecutionError("private-message", [record()])
    stderr = b"private-error\n" + units.diagnostic_suffix(child).encode()
    monkeypatch.setattr(
        units.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 1, b"", stderr),
    )
    with pytest.raises(units.OperatorExecutionError) as caught:
        units.run("node-maintenance", payload, action="start")
    rows = caught.value.failures
    assert [r["helper"] for r in rows] == ["factory-health", "node-maintenance"]
    assert (
        rows[1]["returncode"] == 1
        and rows[1]["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()
    )
    assert len(calls) == 2 and "private-" not in units.diagnostic_suffix(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "helper",
        "outcome",
        "boolean_code",
        "boolean_size",
        "digest",
        "unknown_readiness",
        "deep",
        "schema",
        "duplicate",
        "oversize",
        "invalid_json",
    ],
)
def test_untrusted_child_diagnostic_shape_is_discarded(mutation):
    value = {"schema_version": 1, "failures": [record()]}
    row = value["failures"][0]
    if mutation == "unknown_field":
        row["private"] = "must-not-escape"
    elif mutation == "helper":
        row["helper"] = "/private/command"
    elif mutation == "outcome":
        row["outcome"] = ["private-error"]
    elif mutation == "boolean_code":
        row["returncode"] = True
    elif mutation == "boolean_size":
        row["stdout_bytes"] = True
    elif mutation == "digest":
        row["stderr_sha256"] = "private-error"
    elif mutation == "unknown_readiness":
        row["readiness_errors"] = ["private-error"]
    elif mutation == "deep":
        value["failures"] *= 4
    elif mutation == "schema":
        value["schema_version"] = True
    text = (units.DIAGNOSTIC_PREFIX + json.dumps(value) + "\n").encode()
    if mutation == "duplicate":
        text *= 2
    elif mutation == "oversize":
        text += b"x" * 4097
    elif mutation == "invalid_json":
        text = units.DIAGNOSTIC_PREFIX.encode() + b"{broken"
    assert units.diagnostic_chain(text) == []


def test_readiness_preserves_two_inner_failures_and_only_fixed_codes():
    rows = [record(), copy.deepcopy(record()), copy.deepcopy(record())]
    error = units.readiness_failure(
        "preconditions unavailable",
        {
            "errors": ["factory_status_unavailable"],
            "operator_failures": rows,
            "password": "private-value",
        },
    )
    assert len(error.failures) == 3
    assert error.failures[-1]["readiness_errors"] == ["factory_status_unavailable"]
    assert "private-" not in units.diagnostic_suffix(error)
    refused = units.readiness_failure(
        "admission unavailable", {"errors": ["private-value"]}, admission=True
    )
    assert refused.failures[0]["outcome"] == "admission"
    assert "readiness_errors" not in refused.failures[0]


def test_readiness_blockers_survive_outer_diagnostics_without_private_components():
    error = units.readiness_failure(
        "preconditions unavailable",
        {
            "errors": [],
            "readiness_blockers": ["execution_in_flight", "execution_cleanup_unconfirmed"],
            "components": {"private": "must-not-escape"},
        },
    )
    encoded = units.diagnostic_suffix(error)
    decoded = units.diagnostic_chain(encoded.encode())
    assert decoded[0]["readiness_errors"] == []
    assert decoded[0]["readiness_blockers"] == [
        "execution_in_flight",
        "execution_cleanup_unconfirmed",
    ]
    assert "must-not-escape" not in encoded and "components" not in encoded


@pytest.mark.parametrize(
    "blockers", [["private-message"], [True], {}, "factory_active", ["factory_active"] * 2]
)
def test_malformed_child_blockers_are_discarded(blockers):
    row = units.failure_record("node-health", "preconditions")
    row["readiness_blockers"] = blockers
    frame = units.DIAGNOSTIC_PREFIX + json.dumps({"schema_version": 1, "failures": [row]})
    assert units.diagnostic_chain(frame.encode()) == []
    error = units.readiness_failure("unavailable", {"readiness_blockers": blockers})
    assert "readiness_blockers" not in error.failures[0]


def test_blocker_codes_cannot_be_attached_to_another_helper():
    row = record()
    row["readiness_blockers"] = ["factory_active"]
    frame = units.DIAGNOSTIC_PREFIX + json.dumps({"schema_version": 1, "failures": [row]})
    assert units.diagnostic_chain(frame.encode()) == []


def test_readonly_health_keeps_failed_components_and_bounded_nested_causes(monkeypatch, tmp_path):
    from concurrent.futures import Future
    from contextlib import nullcontext

    health = importlib.import_module("scripts.release.node_health")
    selected = {"release_id": "sd-" + "a" * 12, "configuration_id": "cfg-" + "b" * 24}
    payload = tmp_path / "payload"
    monkeypatch.setattr(units, "guarded", lambda _: None)
    monkeypatch.setattr(
        health, "binding", lambda: (selected, {"coordinator.json": Path("coordinator")}, payload)
    )
    monkeypatch.setattr(
        health,
        "read_json",
        lambda path, *args: (
            {"source_sha": "a" * 40}
            if path.name == "manifest.json"
            else {"platform_token": "private-token", "admission": {}}
        ),
    )
    monkeypatch.setattr(health.journal, "check_ready", lambda: {})
    monkeypatch.setattr(health.proxy_install, "check_binding", lambda *args: None)
    monkeypatch.setattr(health.proxy_health, "observe", lambda _: {})
    monkeypatch.setattr(health, "inspect_unit", lambda *args: {})
    monkeypatch.setattr(health.dependency_install, "installed_endpoints", lambda _: {})

    async def roles(_):
        return {}

    monkeypatch.setattr(health, "roles", roles)
    monkeypatch.setattr(
        health, "http", lambda *args, **kwargs: {"status": "ok", "service": "platform"}
    )
    monkeypatch.setattr(health, "validate_execution", lambda *args: {})
    monkeypatch.setattr(install, "selection", lambda **kwargs: selected)
    pending = {}
    for name in ("broker-health", "outbox-health", "import-health", "factory-health"):
        future = Future()
        row = record()
        row["helper"] = name
        future.set_exception(units.OperatorExecutionError("operator helper unavailable", [row]))
        pending[name] = future
    monkeypatch.setattr(health, "_status_helpers", lambda _: nullcontext(pending))
    observed = health.observe(probe=False)
    assert observed["ready_for_admission"] is False
    assert observed["service_preconditions_met"] is False
    assert observed["errors"] == [
        "broker_metadata_unavailable",
        "outbox_pipeline_unavailable",
        "import_pipeline_unavailable",
        "factory_status_unavailable",
    ]
    error = units.readiness_failure("service preconditions unavailable", observed)
    assert [r["helper"] for r in error.failures] == [
        "broker-health",
        "outbox-health",
        "node-health",
    ]
    assert "private-" not in units.diagnostic_suffix(error)


@pytest.mark.parametrize("failure", ["preconditions", "admission", "nested", "plain"])
def test_failed_lifecycle_cli_preserves_safe_readiness_diagnostics(
    monkeypatch, tmp_path, capsys, failure
):
    lifecycle = importlib.import_module("scripts.release.lifecycle")
    # Only the error path and CLI codec are real here. Fixed helper/kernel/service
    # enforcement and the cause of a prior VM failure need separate evidence.
    selected = {"release_id": "sd-" + "a" * 12, "configuration_id": "cfg-" + "b" * 24}
    payload = tmp_path / selected["release_id"] / "payload"
    state = tmp_path / "operations"
    state.mkdir(mode=0o700)
    (state / "maintenance.lock").touch(mode=0o600)
    monkeypatch.setattr(lifecycle, "STATE", state)
    monkeypatch.setattr(lifecycle, "LAST", state / "maintenance.last.json")
    monkeypatch.setattr(units, "guarded", lambda _: None)
    monkeypatch.setattr(lifecycle.node_health, "binding", lambda: (selected, {}, payload))
    monkeypatch.setattr(lifecycle.preflight, "protected", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle.configuration, "private", lambda _: None)
    monkeypatch.setattr(lifecycle, "digest", lambda _: "ordinary-matching-fixture")
    inner = []
    if failure == "nested":
        inner = [units.failure_record("broker-health", "timeout", error=TimeoutError())]
    original = (
        units.DeliveryError("private-not-for-CLI")
        if failure == "plain"
        else units.readiness_failure(
            "private-not-for-CLI",
            {"errors": ["broker_metadata_unavailable"], "operator_failures": inner},
            admission=failure == "admission",
        )
    )
    expected = copy.deepcopy(getattr(original, "failures", []))
    saved = []

    class Maintenance:
        def __init__(self, *args):
            self.record = {"success": False, "events": []}

        def start(self, *, resume):
            assert resume is False
            raise original

        def save(self, phase):
            saved.append({"phase": phase, **copy.deepcopy(self.record)})

    monkeypatch.setattr(lifecycle, "Maintenance", Maintenance)
    monkeypatch.setattr(sys, "argv", ["lifecycle", "--inside", "--action", "start"])
    with pytest.raises(SystemExit) as caught:
        lifecycle.main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "private-" not in captured.err
    assert captured.err.startswith(
        "Lifecycle refused: maintenance failed; admission not reopened automatically\n"
    )
    assert saved == [
        {
            "phase": "failed",
            "success": False,
            "events": [],
            "failure_code": "maintenance_unconfirmed",
        }
    ]
    assert units.diagnostic_chain(captured.err.encode()) == expected
    if failure != "plain":
        outer = units.OperatorExecutionError("unavailable", expected)
        assert json.loads(units.diagnostic_suffix(outer).split(" ", 1)[1])["failures"] == expected
