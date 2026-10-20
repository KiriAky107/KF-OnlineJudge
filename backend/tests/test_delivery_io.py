"""Ordinary trusted byte fixtures/models; no VM, privilege or execution acceptance."""

import errno
import hashlib
import importlib
import json
import os
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
manifest = importlib.import_module("scripts.release.manifest")
policy = importlib.import_module("scripts.release.policy")
file_copy = importlib.import_module("scripts.release.file_copy")
factory_service = importlib.import_module("scripts.release.factory_service")
proxy_install = importlib.import_module("scripts.release.proxy_install")


@pytest.fixture
def ordinary_bundle(tmp_path):
    """Only manifest-level data, deliberately never used as a runtime image."""
    bundle = tmp_path / "bundle"
    payload = bundle / "payload"
    payload.mkdir(parents=True)
    required = {
        "bin/kf-sandbox",
        "bin/kf-worker",
        "bin/kf-coordinator",
        "python/bin/python3.13",
        "backend/alembic.ini",
        "frontend/index.html",
        "frontend/admin/index.html",
    }
    for name in required:
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ordinary fixture data, never executed\n")
        path.chmod(0o444)
    contract = payload / "deploy/release/contract.v1.json"
    contract.parent.mkdir(parents=True)
    contract.write_bytes((ROOT / "deploy/release/contract.v1.json").read_bytes())
    contract.chmod(0o444)
    images = {}
    for name, toolchain, kind, version in (
        ("gnu", "gnu-cxx-v1", "linux_gnu_cxx_x86_64_v1", "16.2.1+r23+gd564253eb6c8"),
        ("python313", "cpython313-v1", "linux_cpython313_x86_64_v1", "3.13.16"),
        ("java21", "openjdk21-v1", "linux_openjdk21_x86_64_v1", "21.0.12.1+1-LTS"),
    ):
        path = payload / "images" / name / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(json.dumps({"ordinary_inventory_probe": name}).encode())
        path.chmod(0o444)
        images[name] = {
            "toolchain_id": toolchain,
            "image_kind": kind,
            "image_revision": 1,
            "runtime_version": version,
            "image_digest": policy.digest(path),
        }
    for path in sorted(payload.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    payload.chmod(0o555)
    locks = {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in (
            "backend/uv.lock",
            "judge/Cargo.lock",
            "frontend/pnpm-lock.yaml",
            "deploy/release/contract.v1.json",
        )
    }
    locks["deploy/release/contract.v1.json"] = policy.digest(contract)
    manifest.write(bundle, "123456789abc" + "d" * 28, locks, images)
    return bundle, policy.digest(bundle / "manifest.json")


def observed_digests(monkeypatch):
    reads = []
    original = manifest.digest

    def observed(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(manifest, "digest", observed)
    return reads


def test_same_operation_reuses_bytes_but_not_mutable_metadata(ordinary_bundle, monkeypatch):
    bundle, expected = ordinary_bundle
    reads = observed_digests(monkeypatch)
    with manifest.verification_scope():
        first = manifest.verify(bundle, expected)
        first_reads = list(reads)
        assert first_reads
        original_files = list(first["files"])
        first["files"].clear()
        with manifest.verification_scope():
            second = manifest.verify(bundle, expected)
        assert second["files"] == original_files and reads == first_reads
    assert manifest._READ_SCOPE.get() is None
    manifest.verify(bundle, expected)
    assert reads == first_reads * 2


def test_failed_operation_cannot_keep_a_byte_proof(ordinary_bundle, monkeypatch):
    bundle, expected = ordinary_bundle
    reads = observed_digests(monkeypatch)
    with pytest.raises(OSError, match="ordinary control-flow probe"):
        with manifest.verification_scope():
            manifest.verify(bundle, expected)
            raise OSError("ordinary control-flow probe")
    first_reads = list(reads)
    assert manifest._READ_SCOPE.get() is None
    with manifest.verification_scope():
        manifest.verify(bundle, expected)
    assert reads == first_reads * 2


@pytest.mark.parametrize("operator", ["factory", "proxy"])
def test_installer_refusal_reuses_only_its_own_complete_byte_proof(
    ordinary_bundle, monkeypatch, tmp_path, operator
):
    # Model the early operator guard with ordinary owned fixture bytes. No OS
    # identity, installation, service or VM execution is being accepted here.
    bundle, expected = ordinary_bundle
    reads = observed_digests(monkeypatch)
    manifest.verify(bundle, expected)
    complete_read = list(reads)
    reads.clear()

    def refuse(*args, **kwargs):
        assert manifest.verify(bundle, expected) == manifest.verify(bundle, expected)
        raise RuntimeError("ordinary installer refusal probe")

    module = factory_service if operator == "factory" else proxy_install
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "getgid", lambda: 0)
    monkeypatch.setattr(module.preflight, "protected", refuse)
    source = tmp_path / "uncreated-installation-input"
    arguments = [source, expected]
    entry = module.install_unit if operator == "factory" else module.install_proxy
    if operator == "proxy":
        arguments.extend([tmp_path / "unread-certificate", tmp_path / "unread-key"])
    with pytest.raises(RuntimeError, match="ordinary installer refusal probe"):
        entry(*arguments)
    assert reads == complete_read
    assert manifest._READ_SCOPE.get() is None
    assert not source.exists()
    manifest.verify(bundle, expected)
    assert reads == complete_read * 2


@pytest.mark.parametrize("index", range(9))
@pytest.mark.parametrize("phase", ["reuse", "return"])
def test_bundle_identity_model_is_required_through_operation_return(
    ordinary_bundle, monkeypatch, index, phase
):
    # Metadata models only. Actual filesystem mutation and process boundaries
    # are exercised separately in the independently bounded offline VM.
    bundle, expected = ordinary_bundle
    original = manifest._bundle_signature
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        result = original(path)
        if calls >= 3:
            key = "payload/bin/kf-worker"
            stamp = list(result[key])
            stamp[index] += 1
            result[key] = tuple(stamp)
        return result

    monkeypatch.setattr(manifest, "_bundle_signature", changed)
    with pytest.raises(policy.DeliveryError, match="verified bundle changed"):
        with manifest.verification_scope():
            manifest.verify(bundle, expected)
            if phase == "reuse":
                manifest.verify(bundle, expected)
    assert manifest._READ_SCOPE.get() is None


def test_inherited_scope_owner_model_requires_fresh_bytes(ordinary_bundle, monkeypatch):
    bundle, expected = ordinary_bundle
    reads = observed_digests(monkeypatch)
    with manifest.verification_scope():
        manifest.verify(bundle, expected)
        first_reads = list(reads)
        manifest._READ_SCOPE.get().pid = os.getpid() - 1
        manifest.verify(bundle, expected)
        assert reads == first_reads * 2


@pytest.mark.parametrize(
    "limit", ["_MAX_SCOPED_BUNDLES", "_MAX_SCOPED_NODES", "_MAX_SCOPED_METADATA"]
)
def test_scope_capacity_model_falls_back_to_full_reads(ordinary_bundle, monkeypatch, limit):
    bundle, expected = ordinary_bundle
    reads = observed_digests(monkeypatch)
    monkeypatch.setattr(manifest, limit, 0)
    with manifest.verification_scope():
        manifest.verify(bundle, expected)
        first_reads = list(reads)
        manifest.verify(bundle, expected)
    assert reads == first_reads * 2


@pytest.mark.parametrize("length", [0, 17, 3 * 1024**2 + 17])
@pytest.mark.parametrize("mode", ["actual", "unsupported", "copied_prefix_then_unsupported"])
def test_fixed_regular_transfer_preserves_exact_bytes_and_offsets(
    tmp_path, monkeypatch, length, mode
):
    source, target = tmp_path / "source.txt", tmp_path / "target.txt"
    content = (b"ordinary trusted byte transfer\n" * ((length // 30) + 2))[:length]
    source.write_bytes(content)
    original = os.sendfile
    calls = 0

    def transfer(out_fd, in_fd, offset, maximum):
        nonlocal calls
        calls += 1
        if mode == "unsupported" or (mode == "copied_prefix_then_unsupported" and calls > 1):
            # Control-flow model, not evidence of a real kernel failure.
            raise OSError(errno.EINVAL, "ordinary unsupported-path model")
        return original(out_fd, in_fd, offset, maximum)

    monkeypatch.setattr(file_copy.os, "sendfile", transfer)
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        file_copy.transfer(incoming, outgoing, length, deadline=time.monotonic() + 30)
    assert target.read_bytes() == content
    assert policy.digest(target) == policy.digest(source)


def test_copy_inventory_prefetch_is_bounded_and_results_keep_input_order():
    # Trusted callbacks only: ordinary concurrency control, no VM/Root acceptance.
    release, saturated, second = (threading.Event() for _ in range(3))
    lock = threading.Lock()
    consumed, results, errors = [], [], []
    active = maximum = 0

    class Inventory(Sequence):
        def __len__(self):
            return 32

        def __getitem__(self, index):
            if not 0 <= index < len(self):
                raise IndexError
            with lock:
                consumed.append(index)
            return index

    def copy(index):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(active, maximum)
            if active == 4:
                saturated.set()
        try:
            assert release.wait(5)
            if index == 0:
                assert second.wait(5)
            if index == 1:
                second.set()
            return index * 3
        finally:
            with lock:
                active -= 1

    def collect():
        try:
            results.extend(file_copy.bounded_copies(Inventory(), copy))
        except BaseException as error:
            errors.append(error)

    collector = threading.Thread(target=collect)
    collector.start()
    try:
        assert saturated.wait(5)
        with lock:
            assert consumed == [0, 1, 2, 3]
            assert maximum == 4
    finally:
        release.set()
        collector.join(10)
    assert not collector.is_alive() and not errors
    assert results == [index * 3 for index in range(32)]
    assert active == 0 and maximum == 4


def test_copy_failure_joins_dispatched_calls_before_propagating():
    # No files/privileges: observe failure quiescence of trusted callbacks.
    together = threading.Barrier(4)
    release, failing, returned = (threading.Event() for _ in range(3))
    lock = threading.Lock()
    active = 0
    errors, active_when_returned = [], []

    def copy(index):
        nonlocal active
        with lock:
            active += 1
        try:
            if index < 4:
                together.wait(timeout=5)
            if index == 0:
                failing.set()
                raise policy.DeliveryError("ordinary callback refusal model")
            assert release.wait(5)
            return index
        finally:
            with lock:
                active -= 1

    def collect():
        try:
            file_copy.bounded_copies(list(range(32)), copy)
        except BaseException as error:
            errors.append(error)
        finally:
            with lock:
                active_when_returned.append(active)
            returned.set()

    collector = threading.Thread(target=collect)
    collector.start()
    try:
        assert failing.wait(5)
        assert not returned.wait(0.05)
    finally:
        release.set()
        collector.join(10)
    assert not collector.is_alive() and active_when_returned == [0]
    assert len(errors) == 1 and isinstance(errors[0], policy.DeliveryError)
    assert str(errors[0]) == "ordinary callback refusal model"


def test_expired_copy_deadline_refuses_before_dispatch():
    calls = []
    with pytest.raises(policy.DeliveryError, match="copy deadline exceeded"):
        file_copy.bounded_copies(list(range(8)), calls.append, deadline=time.monotonic() - 1)
    assert calls == []
