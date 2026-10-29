"""Ordinary string/worker models, not Root, durability or recovery acceptance."""

import importlib
import random
import re
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
policy = importlib.import_module("scripts.release.policy")
file_copy = importlib.import_module("scripts.release.file_copy")


def old_path_result(value):
    """Frozen pre-optimization lexical policy, independent of the new cache."""
    if not isinstance(value, str) or len(value) > 512:
        return "invalid payload path"
    if not all(re.fullmatch(r"[A-Za-z0-9_.+@-]{1,255}", p) for p in value.split("/")):
        return "noncanonical payload path"
    if any(
        p in {".", "..", ".git", ".localplan", "docs", "__pycache__", ".env", ".ssh"}
        for p in value.split("/")
    ):
        return "private or unsafe payload path"
    if not value.startswith(policy.ROOTS) or value.endswith((".pyc", ".pyo")):
        return "payload path outside delivery allowlist"
    return None


def test_lexical_policy_preserves_boundaries_and_refusal_categories():
    values = [None, True, 1, [], {}, b"backend/a", ""]
    components = [
        ".",
        "..",
        ".git",
        ".localplan",
        "docs",
        "__pycache__",
        ".env",
        ".ssh",
        "a//b",
        "a/",
        "a.pyc",
        "a.pyo",
        "a\x00b",
        "a\nb",
        "é",
        "a" * 255,
        "a" * 256,
        "a" * 250 + "/" + "b" * 250,
        "a" * 250 + "/" + "b" * 254,
    ]
    values.extend("backend/" + component for component in components)
    rng = random.Random(20261028)
    for _ in range(3000):
        value = "".join(rng.choices("abcXYZ019_.+@-/ \\:\n\x00é", k=rng.randrange(90)))
        values.append(rng.choice(["backend/", "images/gnu/", "site/x/", ""]) + value)
    for value in values:
        refused = old_path_result(value)
        if refused is None:
            assert policy.relative_name(value) == value
        else:
            with pytest.raises(policy.DeliveryError) as observed:
                policy.relative_name(value)
            assert str(observed.value) == refused


def test_lexical_cache_is_bounded_and_custom_hash_cannot_bypass_policy():
    class Collision(str):
        def __hash__(self):
            return hash("backend/valid.py")

        def __eq__(self, other):
            return True

    policy._cached_relative_name.cache_clear()
    try:
        policy.relative_name("backend/valid.py")
        before = policy._cached_relative_name.cache_info()
        with pytest.raises(policy.DeliveryError, match="private or unsafe"):
            policy.relative_name(Collision("backend/../escape"))
        assert policy._cached_relative_name.cache_info() == before
        for index in range(20000):
            policy.relative_name("backend/member-" + str(index))
        assert policy._cached_relative_name.cache_info().currsize == 16384
    finally:
        policy._cached_relative_name.cache_clear()


def test_deadline_stops_dispatch_but_waits_for_running_operations():
    started = threading.Barrier(5)
    release = threading.Event()
    finished = threading.Event()
    active = set()
    errors = []
    lock = threading.Lock()

    def operation(index):
        with lock:
            active.add(index)
        try:
            started.wait(timeout=5)
            assert release.wait(5)
            return index
        finally:
            with lock:
                active.remove(index)

    def collect():
        try:
            file_copy.bounded_copies(list(range(32)), operation, deadline=time.monotonic() + 0.2)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    collector = threading.Thread(target=collect)
    collector.start()
    try:
        started.wait(timeout=5)
        assert not finished.wait(0.3)
        with lock:
            assert active == {0, 1, 2, 3}
    finally:
        release.set()
        collector.join(5)
    assert not collector.is_alive() and not active
    assert len(errors) == 1 and isinstance(errors[0], policy.DeliveryError)
    assert "copy deadline exceeded" in str(errors[0])


def test_single_operation_cannot_publish_after_its_deadline(monkeypatch):
    current = [10.0]
    monkeypatch.setattr(file_copy, "time", SimpleNamespace(monotonic=lambda: current[0]))
    calls = []

    def operation(index):
        calls.append(index)
        current[0] = 12.0
        return "completed after deadline"

    with pytest.raises(policy.DeliveryError, match="copy deadline exceeded"):
        file_copy.bounded_copies([0], operation, deadline=11.0)
    assert calls == [0]


def test_iterator_failure_joins_in_flight_work_without_dispatching_rest():
    started, release, finished = (threading.Event() for _ in range(3))
    calls, errors = [], []

    class Broken(Sequence):
        def __len__(self):
            return 32

        def __getitem__(self, index):
            if index == 0:
                return index
            assert started.wait(5)
            raise ValueError("finite inventory refusal")

    def operation(index):
        calls.append(index)
        started.set()
        assert release.wait(5)
        return index

    def collect():
        try:
            file_copy.bounded_copies(Broken(), operation)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    collector = threading.Thread(target=collect)
    collector.start()
    try:
        assert started.wait(5)
        assert not finished.wait(0.05)
    finally:
        release.set()
        collector.join(5)
    assert not collector.is_alive() and calls == [0]
    assert len(errors) == 1 and str(errors[0]) == "finite inventory refusal"


def test_readiness_projection_ports_overlap_with_at_most_two_workers(monkeypatch):
    """Live ordinary threads over modeled ports, not systemd or VM acceptance."""
    node_health = importlib.import_module("scripts.release.node_health")
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=10)
    active = 0
    peak = 0
    started = []
    finished = []
    payload = Path("/ordinary-readiness-port-model")

    def query(name, actual_payload):
        nonlocal active, peak
        assert actual_payload == payload
        with lock:
            active += 1
            peak = max(peak, active)
            started.append(name)
        try:
            barrier.wait()
            return {"component": name}
        finally:
            with lock:
                active -= 1
                finished.append(name)

    monkeypatch.setattr(node_health.operator_units, "run", query)
    with node_health._status_helpers(payload) as pending:
        results = {name: future.result(timeout=15) for name, future in pending.items()}
    names = {"broker-health", "outbox-health", "import-health", "factory-health"}
    assert peak == 2 and active == 0
    assert set(started) == set(finished) == set(results) == names
    assert len(started) == len(finished) == 4
    assert all(results[name] == {"component": name} for name in names)
    assert all(future.done() for future in pending.values())


def test_readiness_projection_failure_keeps_error_and_joins_all_workers(monkeypatch):
    """A consumer error cannot abandon a dispatched ordinary helper model."""
    node_health = importlib.import_module("scripts.release.node_health")
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=10)
    finished = []

    def query(name, payload):
        try:
            barrier.wait()
            if name == "broker-health":
                raise policy.DeliveryError("bounded helper refused")
            return {"component": name}
        finally:
            with lock:
                finished.append(name)

    monkeypatch.setattr(node_health.operator_units, "run", query)
    with pytest.raises(RuntimeError, match="consumer refused observation"):
        with node_health._status_helpers(Path("/ordinary-readiness-port-model")) as pending:
            raise RuntimeError("consumer refused observation")
    assert len(finished) == 4 and set(finished) == set(pending)
    assert all(future.done() for future in pending.values())
    with pytest.raises(policy.DeliveryError, match="bounded helper refused"):
        pending["broker-health"].result()
