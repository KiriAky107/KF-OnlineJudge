"""Ordinary manager transition models; real service cleanup requires the VM."""

import copy
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
units = importlib.import_module("scripts.release.operator_units")
service_install = importlib.import_module("scripts.release.service_install")
UNIT = "kf-broker-health.service"
DESCRIPTION = "KF fixed broker-health sd-" + "a" * 12 + ".cfg-" + "b" * 24
ABSENT = {
    "LoadState": "not-found",
    "ActiveState": "inactive",
    "MainPID": "0",
    "Transient": "no",
    "Description": UNIT,
    "ControlGroup": "",
}
OWNED = {
    **ABSENT,
    "LoadState": "loaded",
    "Transient": "yes",
    "Description": DESCRIPTION,
    "ControlGroup": "/kfonlinejudge.slice/" + UNIT,
}


@pytest.fixture
def group(monkeypatch, tmp_path):
    monkeypatch.setattr(units, "HELPER_CGROUP", tmp_path)
    return tmp_path / UNIT


def populate(group, *, procs="", populated=0):
    group.mkdir()
    (group / "cgroup.procs").write_text(procs)
    (group / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")


def manager(monkeypatch, states, error=None):
    states = iter(states)
    calls = []

    def execute(*args):
        calls.append(args)
        if args[0] == "stop":
            if error:
                raise error
            return ""
        assert args[0] == "show" and args[1] == UNIT
        return "".join(f"{k}={v}\n" for k, v in next(states).items())

    monkeypatch.setattr(service_install, "systemctl", execute)
    return calls


def missing_error():
    return service_install.UnitManagerError(
        ("stop", UNIT),
        subprocess.CompletedProcess([], 5, "", f"Failed to stop {UNIT}: Unit {UNIT} not loaded.\n"),
    )


@pytest.mark.parametrize("state", [ABSENT, OWNED])
@pytest.mark.parametrize("present", [False, True])
def test_already_empty_helper_does_not_race_collection_with_stop(
    monkeypatch, group, state, present
):
    if present:
        populate(group)
    calls = manager(monkeypatch, [state])
    units._cleanup_helper(UNIT, DESCRIPTION)
    assert [x[0] for x in calls] == ["show"]


@pytest.mark.parametrize(
    "changed",
    [
        {"Description": "foreign"},
        {"Transient": "no"},
        {"ControlGroup": "/foreign"},
        {"LoadState": "error"},
        {"ControlGroup": None},
    ],
)
def test_empty_but_foreign_or_unknown_helper_is_never_accepted_or_stopped(
    monkeypatch, group, changed
):
    calls = manager(monkeypatch, [{**OWNED, **changed}])
    with pytest.raises(units.DeliveryError, match="foreign"):
        units._cleanup_helper(UNIT, DESCRIPTION)
    assert [x[0] for x in calls] == ["show"]


@pytest.mark.parametrize("procs,populated", [("45\n", 0), ("", 1)])
def test_disappeared_manager_unit_cannot_hide_residual_descendants(
    monkeypatch, group, procs, populated
):
    populate(group, procs=procs, populated=populated)
    calls = manager(monkeypatch, [ABSENT])
    with pytest.raises(units.DeliveryError, match="cleanup unconfirmed"):
        units._cleanup_helper(UNIT, DESCRIPTION)
    assert [x[0] for x in calls] == ["show"]


def test_missing_kernel_counters_are_not_empty_group_evidence(monkeypatch, group):
    group.mkdir()
    calls = manager(monkeypatch, [ABSENT])
    with pytest.raises(units.DeliveryError, match="cleanup unconfirmed"):
        units._cleanup_helper(UNIT, DESCRIPTION)
    assert len(calls) == 1


@pytest.mark.parametrize("stop_error", [None, "collected"])
def test_active_helper_requires_stop_and_fresh_empty_readback(monkeypatch, group, stop_error):
    calls = manager(
        monkeypatch,
        [{**OWNED, "ActiveState": "active", "MainPID": "42"}, ABSENT],
        missing_error() if stop_error else None,
    )
    units._cleanup_helper(UNIT, DESCRIPTION)
    assert [x[0] for x in calls] == ["show", "stop", "show"]


@pytest.mark.parametrize(
    "after",
    [
        {**ABSENT, "ActiveState": "active"},
        {**ABSENT, "MainPID": "45"},
        {**ABSENT, "ControlGroup": "/foreign"},
        {**OWNED, "Description": "replacement"},
    ],
)
def test_collection_race_still_refuses_live_or_replaced_unit(monkeypatch, group, after):
    calls = manager(
        monkeypatch,
        [{**OWNED, "ActiveState": "active", "MainPID": "42"}, after],
        missing_error(),
    )
    with pytest.raises(units.DeliveryError):
        units._cleanup_helper(UNIT, DESCRIPTION)
    assert [x[0] for x in calls] == ["show", "stop", "show"]


def test_successful_stop_rechecks_ownership(monkeypatch, group):
    calls = manager(
        monkeypatch,
        [
            {**OWNED, "ActiveState": "active", "MainPID": "42"},
            {**OWNED, "Description": "foreign"},
        ],
    )
    with pytest.raises(units.DeliveryError, match="foreign"):
        units._cleanup_helper(UNIT, DESCRIPTION)
    assert len(calls) == 3


def test_ambiguous_manager_error_is_not_absorbed_by_later_absence(monkeypatch, group):
    error = service_install.UnitManagerError(
        ("stop", UNIT), subprocess.CompletedProcess([], 1, "", "denied")
    )
    calls = manager(monkeypatch, [{**OWNED, "ActiveState": "active", "MainPID": "42"}], error)
    with pytest.raises(service_install.UnitManagerError) as caught:
        units._cleanup_helper(UNIT, DESCRIPTION)
    assert caught.value is error and [x[0] for x in calls] == ["show", "stop"]


@pytest.mark.parametrize(
    "arguments,code,stdout,stderr,missing",
    [
        (("stop", UNIT), 5, "", f"Failed to stop {UNIT}: Unit {UNIT} not loaded.\n", True),
        (("stop", UNIT), 1, "", "Failed to connect to bus: denied\n", False),
        (("show", UNIT), 5, "", f"Failed to stop {UNIT}: Unit {UNIT} not loaded.\n", False),
        (
            ("stop", UNIT),
            5,
            "unexpected",
            f"Failed to stop {UNIT}: Unit {UNIT} not loaded.\n",
            False,
        ),
        (("show", UNIT), 0, "x" * 8193, "", False),
    ],
)
def test_manager_failures_keep_fixed_operation_and_digests_only(
    monkeypatch, arguments, code, stdout, stderr, missing
):
    def execute(command, **kwargs):
        assert command == ["/usr/bin/systemctl", *arguments]
        assert kwargs["timeout"] == 15 and kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    monkeypatch.setattr(service_install.subprocess, "run", execute)
    with pytest.raises(service_install.UnitManagerError) as caught:
        service_install.systemctl(*arguments)
    error = caught.value
    assert error.not_loaded is missing
    row = units.failure_record("broker-health", "cleanup", error=error)
    diagnostic = units.diagnostic_suffix(units.OperatorExecutionError("private", [row]))
    assert UNIT not in diagnostic and "denied" not in diagnostic and "unexpected" not in diagnostic
    assert units.diagnostic_chain(diagnostic.encode()) == [row]
    assert row["unit_manager"]["operation"] == arguments[0]
    assert row["unit_manager"]["returncode"] == code


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation", "private-command"),
        ("returncode", True),
        ("not_loaded", "yes"),
        ("stdout_bytes", -1),
        ("stderr_sha256", "private-content"),
        ("unexpected", "private"),
    ],
)
def test_nested_manager_diagnostics_reject_untrusted_shape(field, value):
    row = units.failure_record("broker-health", "cleanup", error=missing_error())
    row = copy.deepcopy(row)
    row["unit_manager"][field] = value
    message = units.DIAGNOSTIC_PREFIX + json.dumps({"schema_version": 1, "failures": [row]})
    assert units.diagnostic_chain(message.encode()) == []
