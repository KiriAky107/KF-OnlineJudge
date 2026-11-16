"""Ordinary orchestration models; actual Root startup remains independent-VM-only."""

import copy
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

lifecycle = importlib.import_module("scripts.release.lifecycle")
units = lifecycle.operator_units


def connect_failure():
    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/delivery/factory-health-not-listening.json"
    )
    output = fixture.read_bytes()
    assert output == b'{"details":null,"error":"factory_health_not_listening"}\n'
    row = units.failure_record(
        "factory-health",
        "exit",
        subprocess.CompletedProcess([], 2, stdout=b"", stderr=output),
        units.DeliveryError("operator helper unavailable"),
    )
    return units.OperatorExecutionError("operator helper unavailable", [row])


def model(monkeypatch, tmp_path, responses):
    m = object.__new__(lifecycle.Maintenance)
    m.selected = {"release_id": "sd-" + "a" * 12, "configuration_id": "cfg-" + "b" * 24}
    m.payload = Path("/ordinary-unused-payload")
    config = tmp_path / "runtime.json"
    config.write_text("{}")
    m.paths = {"runtime.json": config}
    status = {
        "schema_version": 1,
        "configuration_sha256": lifecycle.digest(config),
        "supervisor_authorized": True,
        "execution_gate": "required_per_execution",
        "isolation_verified": False,
        "max_concurrent_runs": 1,
        "active_helpers": 0,
        "cleanup_complete": True,
        "remaining_requests": 4096,
        "remaining_lifetime_seconds": 3600,
    }
    gate = SimpleNamespace(**m.selected, state="draining")
    state = {"ActiveState": "active", "SubState": "running", "MainPID": "123"}
    clock, seen = ([0.0], [])
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lifecycle.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value)
    )
    monkeypatch.setattr(lifecycle.install, "selection", lambda **_: copy.copy(m.selected))
    monkeypatch.setattr(lifecycle.availability, "read", lambda: copy.copy(gate))
    monkeypatch.setattr(m, "unit", lambda name: copy.copy(state))
    monkeypatch.setattr(
        lifecycle.node_health, "inspect_unit", lambda name, selected: seen.append("profile")
    )

    def helper(name, payload):
        assert name == "factory-health" and payload == m.payload
        seen.append("health")
        item = responses.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(status if item == "ready" else item)

    monkeypatch.setattr(units, "run", helper)
    monkeypatch.setattr(
        lifecycle.service_install, "systemctl", lambda *_: pytest.fail("no service restart")
    )
    monkeypatch.setattr(
        lifecycle.availability, "write", lambda *_: pytest.fail("no admission write")
    )
    return (m, clock, seen, gate, state, status)


def test_fresh_factory_observes_typed_connect_failures_then_real_bound_status(
    monkeypatch, tmp_path
):
    m, clock, seen, gate, _, _ = model(
        monkeypatch, tmp_path, [connect_failure(), connect_failure(), "ready"]
    )
    m._wait_for_factory(10, "123")
    assert seen == ["profile", "health"] * 3 and clock == [2.0] and (gate.state == "draining")


@pytest.mark.parametrize(
    "mutation",
    [
        "cleanup",
        "timeout",
        "stderr",
        "stdout",
        "helper",
        "returncode",
        "stdout-bool",
        "unknown",
        "two",
        "authority",
    ],
)
def test_unknown_factory_failure_is_terminal_without_another_observation(
    monkeypatch, tmp_path, mutation
):
    error = connect_failure()
    row = error.failures[0]
    if mutation in ("cleanup", "timeout"):
        row["outcome"] = mutation
    elif mutation == "stderr":
        row["stderr_sha256"] = "c" * 64
    elif mutation == "stdout":
        row["stdout_bytes"] = 1
    elif mutation == "helper":
        row["helper"] = "node-health"
    elif mutation == "returncode":
        row["returncode"] = 1
    elif mutation == "stdout-bool":
        row["stdout_bytes"] = False
    elif mutation == "unknown":
        row["untrusted"] = True
    elif mutation == "two":
        error.failures.append(copy.copy(row))
    elif mutation == "authority":
        row.update(
            units.failure_record(
                "factory-health",
                "exit",
                subprocess.CompletedProcess(
                    [],
                    2,
                    stdout=b"",
                    stderr=b'{"details":null,"error":"factory_health_unavailable"}\n',
                ),
                units.DeliveryError("operator helper unavailable"),
            )
        )
    m, clock, seen, _, _, _ = model(monkeypatch, tmp_path, [error, "ready"])
    with pytest.raises(units.OperatorExecutionError):
        m._wait_for_factory(10, "123")
    assert seen == ["profile", "health"] and clock == [0.0]


@pytest.mark.parametrize("kind", ["configuration", "isolation", "unknown"])
def test_factory_status_authority_is_verified_before_start_confirmation(
    monkeypatch, tmp_path, kind
):
    responses = []
    m, _, seen, _, _, status = model(monkeypatch, tmp_path, responses)
    if kind == "configuration":
        status["configuration_sha256"] = "c" * 64
    elif kind == "isolation":
        status["isolation_verified"] = True
    else:
        status["unknown"] = True
    responses.append(status)
    with pytest.raises(units.DeliveryError):
        m._wait_for_factory(10, "123")
    assert seen == ["profile", "health"]


def test_same_original_deadline_is_not_reset_by_connect_observations(monkeypatch, tmp_path):
    m, clock, seen, _, _, _ = model(monkeypatch, tmp_path, [connect_failure()] * 5)
    with pytest.raises(units.DeliveryError, match="bounded factory readiness"):
        m._wait_for_factory(2.5, "123")
    assert clock == [2.5] and seen.count("health") == 3


def test_slow_success_beyond_original_deadline_cannot_confirm_start(monkeypatch, tmp_path):
    responses = []
    m, clock, seen, _, _, status = model(monkeypatch, tmp_path, responses)

    def delayed():
        clock[0] = 10
        return status

    responses.append(delayed)
    with pytest.raises(units.DeliveryError, match="bounded factory readiness"):
        m._wait_for_factory(10, "123")
    assert seen == ["profile", "health"]


def test_factory_observations_have_finite_count_even_with_long_unit_deadline(monkeypatch, tmp_path):
    m, clock, seen, _, _, _ = model(monkeypatch, tmp_path, [connect_failure()] * 121)
    with pytest.raises(units.DeliveryError, match="bounded factory readiness"):
        m._wait_for_factory(1800, "123")
    assert seen.count("health") == 120 and clock == [120.0]


@pytest.mark.parametrize("change", ["gate", "binding", "unit", "profile"])
def test_changed_startup_authority_cannot_be_waited_away(monkeypatch, tmp_path, change):
    m, _, seen, gate, state, _ = model(monkeypatch, tmp_path, ["ready"])
    if change == "gate":
        gate.state = "open"
    elif change == "binding":
        monkeypatch.setattr(lifecycle.install, "selection", lambda **_: {})
    elif change == "unit":
        state["ActiveState"] = "failed"
    else:

        def denied(*_):
            raise units.DeliveryError("profile changed")

        monkeypatch.setattr(lifecycle.node_health, "inspect_unit", denied)
    with pytest.raises(units.DeliveryError):
        m._wait_for_factory(10, "123")
    assert "health" not in seen


@pytest.mark.parametrize("pid", ["0", "1", True, 123])
def test_initial_factory_PID_must_be_the_exact_manager_string(monkeypatch, tmp_path, pid):
    m, _, seen, _, _, _ = model(monkeypatch, tmp_path, ["ready"])
    with pytest.raises(units.DeliveryError, match="process identity"):
        m._wait_for_factory(10, pid)
    assert "health" not in seen


@pytest.mark.parametrize("first_ready", [False, True])
def test_factory_PID_change_is_terminal_before_or_after_status_reply(
    monkeypatch, tmp_path, first_ready
):
    responses = []
    m, _, seen, _, state, status = model(monkeypatch, tmp_path, responses)

    def replaced():
        state["MainPID"] = "124"
        return status if first_ready else connect_failure()

    responses.extend([replaced, "ready"])
    with pytest.raises(units.DeliveryError, match="process changed"):
        m._wait_for_factory(10, "123")
    assert seen == ["profile", "health"]
