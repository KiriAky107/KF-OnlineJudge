"""Ordinary orchestration models; real helpers and VM startup require separate evidence."""

import copy
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

lifecycle = importlib.import_module("scripts.release.lifecycle")
units = importlib.import_module("scripts.release.operator_units")


def health(*, timeout=False, admission=True):
    value = {
        "schema_version": 1,
        "source_sha": "a" * 40,
        "configuration_id": "cfg-" + "b" * 24,
        "service_preconditions_met": not timeout,
        "ready_for_admission": not timeout and admission,
        "isolation_verified": False,
        "errors": ["broker_metadata_unavailable"] if timeout else [],
    }
    if timeout:
        value["operator_failures"] = [
            units.failure_record(
                "broker-health",
                "exit",
                subprocess.CompletedProcess(
                    [],
                    1,
                    stdout=b"",
                    stderr=(b"Broker health refused: broker snapshot query exceeded deadline\n"),
                ),
                units.DeliveryError("operator helper unavailable"),
            )
        ]
    return value


def harness(monkeypatch, responses, *, gate_change=None):
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = {
        "release_id": "sd-" + "a" * 12,
        "configuration_id": "cfg-" + "b" * 24,
    }
    maintenance.payload = Path("/ordinary-model/payload")
    maintenance.record = {
        "schema_version": 1,
        "source_sha": "a" * 40,
        "configuration_id": maintenance.selected["configuration_id"],
        "action": "start",
        "phase": "prepared",
        "success": False,
        "events": [],
        "root_cleanup_confirmed": False,
        "boot_id": "11111111-1111-4111-8111-111111111111",
    }
    gate = SimpleNamespace(**maintenance.selected, state="quiescent")
    observed = []

    def write(selected, state):
        assert selected == maintenance.selected
        gate.state = state
        observed.append(("gate", state))

    def read():
        observed.append(("gate-read", gate.state))
        if gate_change:
            return gate_change(copy.copy(gate))
        return copy.copy(gate)

    def start_group(names):
        observed.append(("start-group", names))
        maintenance.record["phase"] = "start_confirmed"

    def save(phase):
        maintenance.record["phase"] = phase
        lifecycle.validate_record(maintenance.record)
        observed.append(("save", copy.deepcopy(maintenance.record)))

    remaining = iter(responses)

    def helper(name, payload):
        assert name == "node-health" and payload == maintenance.payload
        observed.append(("health", gate.state))
        item = next(remaining)
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)

    monkeypatch.setattr(lifecycle.availability, "write", write)
    monkeypatch.setattr(lifecycle.availability, "read", read)
    monkeypatch.setattr(maintenance, "require_fresh_start", lambda: observed.append(("fresh",)))
    monkeypatch.setattr(maintenance, "start_group", start_group)
    monkeypatch.setattr(maintenance, "save", save)
    monkeypatch.setattr(
        maintenance, "close_failed_admission", lambda: write(maintenance.selected, "draining")
    )
    monkeypatch.setattr(units, "run", helper)
    return maintenance, observed, gate


def test_exact_timeout_rechecks_once_with_gate_closed_and_no_service_restart(monkeypatch):
    m, seen, gate = harness(monkeypatch, [health(timeout=True), health(admission=False), health()])
    m.start_locked(resume=False)
    assert [x for x in seen if x[0] == "health"] == [
        ("health", "draining"),
        ("health", "draining"),
        ("health", "open"),
    ]
    assert [x[1] for x in seen if x[0] == "start-group"] == list(lifecycle.START_GROUPS)
    recorded = next(i for i, x in enumerate(seen) if x[0] == "save")
    checks = [i for i, x in enumerate(seen) if x[0] == "health"]
    assert checks[0] < recorded < checks[1]
    assert seen[recorded][1]["readiness_rechecks"] == 1
    assert m.record["phase"] == "accepting" and gate.state == "open"


def test_clean_start_keeps_two_health_reads_and_no_recheck_record(monkeypatch):
    m, seen, gate = harness(monkeypatch, [health(admission=False), health()])
    m.start_locked(resume=False)
    assert [x for x in seen if x[0] == "health"] == [("health", "draining"), ("health", "open")]
    assert "readiness_rechecks" not in m.record and gate.state == "open"


def test_second_timeout_is_terminal_and_never_opens_admission(monkeypatch):
    m, seen, gate = harness(monkeypatch, [health(timeout=True), health(timeout=True)])
    with pytest.raises(units.OperatorExecutionError):
        m.start_locked(resume=False)
    assert len([x for x in seen if x[0] == "health"]) == 2
    assert ("gate", "open") not in seen and gate.state == "draining"
    assert m.record["readiness_rechecks"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "authority",
        "two-errors",
        "configuration",
        "source",
        "version-bool",
        "cleanup",
        "manager-timeout",
        "other-query",
        "stdout",
        "code-bool",
        "size-bool",
        "unknown-field",
        "second-failure",
        "missing-diagnostics",
    ],
)
def test_other_failures_never_receive_the_transient_recheck(monkeypatch, mutation):
    value = health(timeout=True)
    row = value["operator_failures"][0]
    if mutation == "authority":
        value["errors"] = ["db_authority_unavailable"]
    elif mutation == "two-errors":
        value["errors"].append("factory_status_unavailable")
    elif mutation == "configuration":
        value["configuration_id"] = "cfg-" + "c" * 24
    elif mutation == "source":
        value["source_sha"] = "c" * 40
    elif mutation == "version-bool":
        value["schema_version"] = True
    elif mutation == "cleanup":
        row["outcome"] = "cleanup"
    elif mutation == "manager-timeout":
        row["outcome"] = "timeout"
    elif mutation == "other-query":
        row["stderr_sha256"] = "c" * 64
    elif mutation == "stdout":
        row["stdout_bytes"] = 1
    elif mutation == "code-bool":
        row["returncode"] = True
    elif mutation == "size-bool":
        row["stdout_bytes"] = False
    elif mutation == "unknown-field":
        row["untrusted"] = True
    elif mutation == "second-failure":
        value["operator_failures"].append(copy.deepcopy(row))
    elif mutation == "missing-diagnostics":
        del value["operator_failures"]
    m, seen, gate = harness(monkeypatch, [value])
    with pytest.raises(units.OperatorExecutionError):
        m.start_locked(resume=False)
    assert [x for x in seen if x[0] == "health"] == [("health", "draining")]
    assert "readiness_rechecks" not in m.record and gate.state == "draining"


@pytest.mark.parametrize("change", ["absent", "open", "wrong-release", "wrong-config"])
def test_recheck_requires_actual_selected_closed_gate(monkeypatch, change):
    def changed(gate):
        if change == "absent":
            return None
        if change == "open":
            gate.state = "open"
        elif change == "wrong-release":
            gate.release_id = "sd-" + "c" * 12
        elif change == "wrong-config":
            gate.configuration_id = "cfg-" + "c" * 24
        return gate

    m, seen, _ = harness(monkeypatch, [health(timeout=True)], gate_change=changed)
    with pytest.raises(units.DeliveryError, match="selected closed admission"):
        m.start_locked(resume=False)
    assert len([x for x in seen if x[0] == "health"]) == 1
    assert ("gate", "open") not in seen and "readiness_rechecks" not in m.record


def test_post_open_failure_still_closes_admission_without_recheck(monkeypatch):
    m, seen, gate = harness(monkeypatch, [health(admission=False), health(timeout=True)])
    with pytest.raises(units.OperatorExecutionError):
        m.start_locked(resume=False)
    assert len([x for x in seen if x[0] == "health"]) == 2
    assert [x for x in seen if x[0] == "gate"] == [
        ("gate", "draining"),
        ("gate", "open"),
        ("gate", "draining"),
    ]
    assert gate.state == "draining" and "readiness_rechecks" not in m.record


def test_already_recorded_recheck_cannot_repeat(monkeypatch):
    m, seen, gate = harness(monkeypatch, [health(timeout=True)])
    m.record["readiness_rechecks"] = 1
    with pytest.raises(units.OperatorExecutionError):
        m.start_locked(resume=False)
    assert len([x for x in seen if x[0] == "health"]) == 1
    assert gate.state == "draining" and m.record["readiness_rechecks"] == 1


def test_helper_dispatch_or_cleanup_exception_is_not_retried(monkeypatch):
    error = units.OperatorExecutionError("ordinary unconfirmed helper cleanup")
    m, seen, gate = harness(monkeypatch, [error])
    with pytest.raises(units.OperatorExecutionError) as caught:
        m.start_locked(resume=False)
    assert caught.value is error and gate.state == "draining"
    assert len([x for x in seen if x[0] == "health"]) == 1
    assert "readiness_rechecks" not in m.record


@pytest.mark.parametrize("value", [0, 2, True, "1", None, []])
def test_receipt_recheck_count_is_exactly_one_integer(monkeypatch, value):
    m, _, _ = harness(monkeypatch, [])
    m.record["readiness_rechecks"] = value
    with pytest.raises(units.DeliveryError, match="readiness recheck"):
        lifecycle.validate_record(m.record)


@pytest.mark.parametrize("action", ["drain", "stop"])
def test_non_start_action_cannot_adopt_a_recheck_receipt(monkeypatch, action):
    m, _, _ = harness(monkeypatch, [])
    m.record.update(action=action, readiness_rechecks=1)
    with pytest.raises(units.DeliveryError, match="readiness recheck"):
        lifecycle.validate_record(m.record)
