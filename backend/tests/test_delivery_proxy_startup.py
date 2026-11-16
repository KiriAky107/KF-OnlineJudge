"""Ordinary modeled lifecycle checks; actual HTTPS and service gates require the VM."""

import importlib
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
lifecycle = importlib.import_module("scripts.release.lifecycle")
proxy_health = importlib.import_module("scripts.release.proxy_health")
DeliveryError = importlib.import_module("scripts.release.policy").DeliveryError
READY = {
    "reachable": True,
    "pinned_certificate_and_hostname_verified": True,
    "public_browser_trust_verified": False,
}


def setup_maintenance(monkeypatch, *, active=False):
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = {"release_id": "sd-123456789abc", "configuration_id": "cfg-" + "1" * 24}
    maintenance.record = {"events": []}
    clock = [100.0]
    state = {"active": active}
    saves = []
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lifecycle.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value)
    )
    monkeypatch.setattr(lifecycle.install, "selection", lambda **_: maintenance.selected.copy())
    monkeypatch.setattr(lifecycle.node_health, "inspect_unit", lambda *_: None)
    monkeypatch.setattr(lifecycle.proxy_install, "check_binding", lambda *_: None)
    monkeypatch.setattr(
        maintenance,
        "unit",
        lambda _: {
            "ActiveState": "active" if state["active"] else "inactive",
            "SubState": "running",
            "MainPID": "42" if state["active"] else "0",
        },
    )
    monkeypatch.setattr(
        maintenance,
        "save",
        lambda phase: saves.append((phase, [x.copy() for x in maintenance.record["events"]])),
    )

    def systemctl(*args):
        assert args == ("--no-block", "start", "kf-proxy.service")
        state["active"] = True
        return ""

    monkeypatch.setattr(lifecycle.service_install, "systemctl", systemctl)
    return maintenance, clock, saves


@pytest.mark.parametrize(
    "group,active", [(True, False), (False, False), (True, True), (False, True)]
)
def test_proxy_process_is_not_confirmed_until_actual_https_probe_succeeds(
    monkeypatch, group, active
):
    maintenance, clock, saves = setup_maintenance(monkeypatch, active=active)
    probes = []

    def probe(selected, *, timeout):
        assert all(not x["confirmed"] for x in maintenance.record["events"])
        probes.append(timeout)
        if len(probes) == 1:
            raise proxy_health.HTTPSNotReady("launcher has not served HTTPS yet")
        return READY.copy()

    monkeypatch.setattr(proxy_health, "observe", probe)
    if group:
        maintenance.start_group(("proxy",))
    else:
        maintenance.change("proxy", "start")
    assert probes == [5.0, 5.0] and clock[0] == 100.5
    if not active:
        assert saves[0][1][0]["confirmed"] is False
        assert saves[-1][1][0]["confirmed"] is True


@pytest.mark.parametrize(
    "error", [ssl.SSLCertVerificationError("bad certificate"), DeliveryError("changed TLS binding")]
)
def test_proxy_security_failure_is_fatal_without_wait_or_confirmation(monkeypatch, error):
    maintenance, clock, _ = setup_maintenance(monkeypatch)
    calls = []

    def probe(*_, **__):
        calls.append(1)
        raise error

    monkeypatch.setattr(proxy_health, "observe", probe)
    with pytest.raises(type(error)):
        maintenance.start_group(("proxy",))
    assert calls == [1] and clock[0] == 100
    assert maintenance.record["events"] == [
        {"unit": "proxy", "operation": "start", "confirmed": False}
    ]


@pytest.mark.parametrize("late_ready", [False, True])
def test_proxy_wait_cannot_restart_or_succeed_after_original_deadline(monkeypatch, late_ready):
    maintenance, clock, _ = setup_maintenance(monkeypatch)
    probes = []

    def unavailable(*_, timeout):
        probes.append(timeout)
        clock[0] += 131
        if late_ready:
            return READY.copy()
        raise proxy_health.HTTPSNotReady("still unavailable")

    monkeypatch.setattr(proxy_health, "observe", unavailable)
    with pytest.raises(DeliveryError, match="bounded HTTPS startup unconfirmed"):
        maintenance.start_group(("proxy",))
    assert probes == [5.0]
    assert maintenance.record["events"][0]["confirmed"] is False


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError("not listening"), TimeoutError("not ready")]
)
def test_proxy_probe_labels_only_transient_transport_failure(monkeypatch, error):
    def unavailable(*_, **__):
        raise error

    monkeypatch.setattr(proxy_health, "_observe", unavailable)
    with pytest.raises(proxy_health.HTTPSNotReady):
        proxy_health.observe({})


@pytest.mark.parametrize("timeout", [0, -1, 5.01, True, float("nan")])
def test_proxy_probe_rejects_unbounded_timeout(timeout):
    with pytest.raises(DeliveryError, match="timeout exceeds bound"):
        proxy_health.observe({}, timeout=timeout)
