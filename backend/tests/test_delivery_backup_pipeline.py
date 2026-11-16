"""Trusted ordinary bytes and bounded threads; no privileged backup or VM proof."""

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from scripts.release import backup_objects as objects
from scripts.release.policy import DeliveryError


@pytest.fixture
def capture_files(tmp_path):
    incoming = tmp_path / "source"
    incoming.mkdir()
    target = tmp_path / "objects"
    target.mkdir(mode=0o700)
    data = [bytes(range(256)) * 256 + bytes([i]) for i in range(4)]
    paths = []
    for i, body in enumerate(data):
        path = incoming / str(i)
        path.write_bytes(body)
        paths.append(path)
    return paths, data, target, {}, {}


def rows(paths):
    return [(path, path.stat()) for path in paths]


def store(paths, target, seen, captured, executor, *, remaining=1024**2):
    return objects.store_batch(
        rows(paths),
        target,
        seen,
        captured,
        executor,
        time.monotonic() + 20,
        remaining_bytes=remaining,
    )


def test_four_complete_source_reads_have_one_serial_object_owner(capture_files, monkeypatch):
    paths, data, target, seen, captured = capture_files
    actual = objects.hash_source
    barrier = threading.Barrier(4)
    mutex = threading.Lock()
    active, peak, complete = 0, 0, []

    def hash_source(path, before, deadline):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(active, peak)
        try:
            barrier.wait(timeout=5)
            result = actual(path, before, deadline)
            complete.append(path)
            return result
        finally:
            with mutex:
                active -= 1

    monkeypatch.setattr(objects, "hash_source", hash_source)
    with ThreadPoolExecutor(max_workers=4) as executor:
        result = store(paths, target, seen, captured, executor)
    assert peak == 4 and active == 0 and set(complete) == set(paths)
    assert result == [(hashlib.sha256(body).hexdigest(), len(body)) for body in data]
    assert len(seen) == len(captured) == len(list(target.iterdir())) == 4
    for body in data:
        path = target / hashlib.sha256(body).hexdigest()
        assert path.read_bytes() == body and path.stat().st_nlink == 1
        assert path.stat().st_mode & 0o777 == 0o600
    assert not (target / ".pending").exists()


def test_each_logical_source_is_hashed_even_when_all_objects_already_exist(
    capture_files, monkeypatch
):
    paths, data, target, seen, captured = capture_files
    with ThreadPoolExecutor(max_workers=4) as executor:
        store(paths, target, seen, captured, executor)
        actual, complete = objects.hash_source, []

        def hash_source(path, before, deadline):
            result = actual(path, before, deadline)
            complete.append((path, result.digest))
            return result

        monkeypatch.setattr(objects, "hash_source", hash_source)
        monkeypatch.setattr(
            objects,
            "same_object_bytes",
            lambda *a: pytest.fail("captured object reread for comparison"),
        )
        result = store(paths, target, seen, captured, executor)
    assert set(complete) == {
        (path, hashlib.sha256(body).hexdigest()) for path, body in zip(paths, data, strict=True)
    }
    assert result == [(hashlib.sha256(body).hexdigest(), 0) for body in data]
    assert len(list(target.iterdir())) == 4


@pytest.mark.parametrize("change", ["source", "path", "deadline", "object"])
def test_hash_binding_and_current_object_identity_remain_mandatory(capture_files, change):
    paths, data, target, seen, captured = capture_files
    path = paths[0]
    with ThreadPoolExecutor(max_workers=4) as executor:
        store(paths, target, seen, captured, executor)
    before = path.stat()
    deadline = time.monotonic() + 20
    fingerprint = objects.hash_source(path, before, deadline)
    if change == "source":
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
    elif change == "path":
        path = paths[1]
    elif change == "deadline":
        deadline = time.monotonic() - 1
    else:
        (target / fingerprint.digest).write_bytes(data[0][:-1] + b"!")
    with pytest.raises(DeliveryError):
        objects.store(
            path,
            before,
            target,
            seen,
            deadline,
            remaining_bytes=1024**2,
            _prehashed=fingerprint,
            _captured=captured,
        )
    assert not (target / ".pending").exists()


def test_worker_failure_joins_every_started_hash_and_publishes_no_batch_objects(
    capture_files, monkeypatch
):
    paths, _, target, seen, captured = capture_files
    actual = objects.hash_source
    barrier = threading.Barrier(4)
    complete = []

    def hash_source(path, before, deadline):
        barrier.wait(timeout=5)
        try:
            if path == paths[1]:
                raise DeliveryError("trusted fixture refused")
            return actual(path, before, deadline)
        finally:
            complete.append(path)

    monkeypatch.setattr(objects, "hash_source", hash_source)
    with ThreadPoolExecutor(max_workers=4) as executor:
        with pytest.raises(DeliveryError, match="trusted fixture refused"):
            store(paths, target, seen, captured, executor)
    assert set(complete) == set(paths)
    assert not seen and not captured and not list(target.iterdir())


@pytest.mark.parametrize("count", [0, 5])
def test_window_bound_is_checked_before_any_submission(capture_files, monkeypatch, count):
    paths, _, target, seen, captured = capture_files
    monkeypatch.setattr(objects, "hash_source", lambda *a: pytest.fail("invalid window dispatched"))
    selected = [] if count == 0 else paths + [paths[0]]
    with ThreadPoolExecutor(max_workers=4) as executor:
        with pytest.raises(DeliveryError, match="concurrency"):
            store(selected, target, seen, captured, executor)
    assert not list(target.iterdir())


def test_free_space_floor_is_rechecked_for_each_serial_store(capture_files, monkeypatch):
    paths, data, target, seen, captured = capture_files
    original = os.statvfs
    calls = []

    def available(path):
        if path == target:
            calls.append(path)
            space = 64 * 1024**2 + (4 * len(data[0]) if len(calls) == 1 else len(data[0]) - 1)
            return SimpleNamespace(f_frsize=1, f_bavail=space)
        return original(path)

    monkeypatch.setattr(objects.os, "statvfs", available)
    with ThreadPoolExecutor(max_workers=4) as executor:
        with pytest.raises(DeliveryError, match="capacity exhausted"):
            store(paths, target, seen, captured, executor)
    assert len(calls) == 2 and len(seen) == len(captured) == len(list(target.iterdir())) == 1
    assert not (target / ".pending").exists()


def test_unique_object_byte_budget_is_serial_and_not_multiplied_by_workers(capture_files):
    paths, data, target, seen, captured = capture_files
    with ThreadPoolExecutor(max_workers=4) as executor:
        with pytest.raises(DeliveryError, match="physical capacity exhausted"):
            store(
                paths, target, seen, captured, executor, remaining=len(data[0]) + len(data[1]) - 1
            )
    assert len(seen) == len(captured) == len(list(target.iterdir())) == 1
    assert not (target / ".pending").exists()
