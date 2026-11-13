"""Modeled publication/failure order; Root ownership and live stop proofs require VM."""

import importlib
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
availability = importlib.import_module("scripts.release.availability")
install = importlib.import_module("scripts.release.install")
dependency_install = importlib.import_module("scripts.release.dependency_install")
DeliveryError = importlib.import_module("scripts.release.policy").DeliveryError
AvailabilityRecord = availability.AvailabilityRecord


@pytest.fixture
def activation_gate(tmp_path, monkeypatch):
    prior = {"release_id": "sd-111111111111", "configuration_id": "cfg-" + "1" * 24}
    target = {"release_id": "sd-222222222222", "configuration_id": "cfg-" + "2" * 24}
    active = tmp_path / "active.json"
    active.write_text(json.dumps(prior))
    gate = tmp_path / "admission" / "status.json"
    gate.parent.mkdir()
    events = []
    monkeypatch.setattr(availability, "GATE", gate)
    monkeypatch.setattr(install, "ACTIVE", active)
    monkeypatch.setattr(install, "CURRENT", tmp_path / "current")
    monkeypatch.setattr(availability, "require_root", lambda: None)
    monkeypatch.setattr(install.preflight, "protected", lambda *a, **k: None)
    monkeypatch.setattr(install, "directory", lambda *a: None)
    monkeypatch.setattr(install, "selection", lambda **k: json.loads(active.read_text()))
    monkeypatch.setattr(install, "require_stopped", lambda: events.append("apps-stopped"))
    monkeypatch.setattr(
        dependency_install,
        "require_stopped",
        lambda: events.append("dependencies-stopped"),
    )

    def read():
        if not gate.exists():
            return None
        return AvailabilityRecord.model_validate_json(gate.read_bytes())

    monkeypatch.setattr(availability, "read", read)
    return SimpleNamespace(
        prior=prior, target=target, active=active, gate=gate, events=events, read=read
    )


def gate_value(selected, state):
    return {"schema_version": 1, **selected, "state": state}


@pytest.mark.parametrize("state", ["open", "draining", "quiescent"])
def test_stopped_activation_publishes_only_target_closed_binding(activation_gate, state):
    model = activation_gate
    model.gate.write_text(json.dumps(gate_value(model.prior, state)))
    before_selection = model.active.read_bytes()
    result = availability.prepare_activation(model.prior, model.target)
    assert result == gate_value(model.target, "draining")
    assert model.read().state == "draining"
    assert model.active.read_bytes() == before_selection
    assert model.events == ["dependencies-stopped"]
    assert stat.S_IMODE(model.gate.stat().st_mode) == 0o644
    assert list(model.gate.parent.iterdir()) == [model.gate]


def test_first_activation_requires_no_orphan_gate(activation_gate):
    model = activation_gate
    model.active.unlink()
    assert availability.prepare_activation(None, model.target) == gate_value(
        model.target, "draining"
    )


@pytest.mark.parametrize("case", ["foreign", "orphan", "missing"])
def test_unknown_prior_admission_refuses_before_publication(activation_gate, case):
    model = activation_gate
    if case != "missing":
        selected = model.target if case == "foreign" else model.prior
        model.gate.write_text(json.dumps(gate_value(selected, "open")))
    if case == "orphan":
        model.active.unlink()
    before = model.gate.read_bytes() if model.gate.exists() else None
    with pytest.raises(DeliveryError):
        availability.prepare_activation(None if case == "orphan" else model.prior, model.target)
    assert (model.gate.read_bytes() if model.gate.exists() else None) == before
    assert model.events == []


def test_dependency_stop_refusal_keeps_old_gate_and_selection(activation_gate, monkeypatch):
    model = activation_gate
    model.gate.write_text(json.dumps(gate_value(model.prior, "open")))
    before = model.gate.read_bytes(), model.active.read_bytes()

    def running():
        raise DeliveryError("all dependency units must be stopped")

    monkeypatch.setattr(dependency_install, "require_stopped", running)
    with pytest.raises(DeliveryError, match="dependency units"):
        availability.prepare_activation(model.prior, model.target)
    assert (model.gate.read_bytes(), model.active.read_bytes()) == before


@pytest.mark.parametrize("during_creation", [False, True])
def test_selection_drift_cannot_publish_a_target_gate(
    activation_gate, monkeypatch, during_creation
):
    model = activation_gate
    model.gate.write_text(json.dumps(gate_value(model.prior, "open")))
    before = model.gate.read_bytes()
    original = install.exclusive_file

    def changed(path, contents, mode):
        original(path, contents, mode)
        model.active.write_text(json.dumps(model.target))

    if during_creation:
        monkeypatch.setattr(install, "exclusive_file", changed)
    else:
        model.active.write_text(json.dumps(model.target))
    with pytest.raises(DeliveryError, match="availability changed"):
        availability.prepare_activation(model.prior, model.target)
    assert model.gate.read_bytes() == before
    assert list(model.gate.parent.iterdir()) == [model.gate]


@pytest.mark.parametrize("old_state", ["open", "draining"])
@pytest.mark.parametrize("state", ["open", "draining", "quiescent"])
def test_ordinary_write_still_refuses_foreign_nonquiescent_binding(
    activation_gate, old_state, state
):
    model = activation_gate
    model.gate.write_text(json.dumps(gate_value(model.prior, old_state)))
    model.active.write_text(json.dumps(model.target))
    before = model.gate.read_bytes()
    with pytest.raises(DeliveryError, match="replacement requires quiescence"):
        availability.write(model.target, state)
    assert model.gate.read_bytes() == before


def test_ordinary_write_retains_prior_quiescent_transition(activation_gate):
    model = activation_gate
    model.gate.write_text(json.dumps(gate_value(model.prior, "quiescent")))
    model.active.write_text(json.dumps(model.target))
    assert availability.write(model.target, "draining") == gate_value(model.target, "draining")


def test_closed_target_gate_remains_closed_when_later_pointer_publication_fails(
    activation_gate,
):
    model = activation_gate
    model.gate.write_text(json.dumps(gate_value(model.prior, "open")))
    availability.prepare_activation(model.prior, model.target)
    # The installer may now fail before or between pointer writes. The old
    # selected API sees a mismatched gate; the new API sees draining. Neither
    # selection has an open record. This does not assert a completed drain.
    record = model.read()
    assert (record.release_id, record.configuration_id) != (
        model.prior["release_id"],
        model.prior["configuration_id"],
    )
    assert record.state == "draining"
    model.active.write_text(json.dumps(model.target))
    assert record.state == "draining"
