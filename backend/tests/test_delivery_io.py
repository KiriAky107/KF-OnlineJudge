"""Ordinary trusted byte fixtures/models; no VM, privilege or execution acceptance."""

import errno
import hashlib
import importlib
import json
import os
import sys
import threading
import time
from collections import Counter
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
setup_session = importlib.import_module("scripts.release.setup_session")
lifecycle = importlib.import_module("scripts.release.lifecycle")
installer = importlib.import_module("scripts.release.install")


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
    original_payload = manifest._read_payload

    def observed(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(manifest, "digest", observed)

    def payload(row):
        reads.append(row[0])
        return original_payload(row)

    monkeypatch.setattr(manifest, "_read_payload", payload)
    return reads


@pytest.fixture
def ordinary_installation_model(ordinary_bundle, monkeypatch, tmp_path):
    """Owned dummy bytes; Root accounts, kernel and ownership gates are models."""
    bundle, _ = ordinary_bundle
    data = json.loads((bundle / "manifest.json").read_bytes())
    for name, contents in (
        ("deploy/release/kfonlinejudge.slice", b"ordinary unit fixture, never started\n"),
        ("images/gnu/bin/ordinary-data", b"ordinary image bytes, never executed\n"),
    ):
        path = bundle / "payload" / name
        path.parent.parent.chmod(0o755)
        path.parent.mkdir(mode=0o755, exist_ok=True)
        path.parent.chmod(0o755)
        path.write_bytes(contents)
        path.chmod(0o444)
    for path in sorted((bundle / "payload").rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    (bundle / "manifest.json").unlink()
    manifest.write(bundle, data["source_sha"], data["locks"], data["images"])
    expected = policy.digest(bundle / "manifest.json")
    root = tmp_path / "ordinary-installation-model"
    (root / "etc/systemd/system").mkdir(parents=True)

    def fixture_path(value):
        value = Path(value)
        if value.is_absolute() and value.parts[1:2] in (("opt",), ("etc",), ("var",)):
            return root / value.relative_to("/")
        return value

    def directory(path, mode):
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        path.chmod(mode)

    monkeypatch.setattr(installer, "Path", fixture_path)
    monkeypatch.setattr(installer, "RELEASES", root / "opt/kf-onlinejudge/releases")
    monkeypatch.setattr(installer, "IMAGES", root / "var/lib/kf-judge/images")
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "ensure_accounts", lambda: None)
    monkeypatch.setattr(installer.preflight, "protected", lambda *a, **k: None)
    monkeypatch.setattr(installer.preflight, "observe", lambda *a, **k: {"eligible": True})
    monkeypatch.setattr(installer, "directory", directory)
    monkeypatch.setattr(installer, "data_directory", lambda path, owner: directory(path, 0o700))
    data = json.loads((bundle / "manifest.json").read_bytes())
    return {
        "bundle": bundle,
        "expected": expected,
        "data": data,
        "root": root,
        "release": installer.RELEASES / data["release_id"],
        "stage": installer.RELEASES / ("." + data["release_id"] + ".installing"),
        "images": installer.IMAGES / data["release_id"],
    }


def test_new_staged_install_reads_each_destination_once(ordinary_installation_model, monkeypatch):
    model = ordinary_installation_model
    reads = []
    original = manifest._read_payload

    def read(row):
        if model["stage"] in row[0].parents:
            reads.append(row[0].relative_to(model["stage"] / "payload").as_posix())
        return original(row)

    monkeypatch.setattr(manifest, "_read_payload", read)
    receipt = installer.files(model["bundle"], model["expected"])
    assert Counter(reads) == Counter(row["path"] for row in model["data"]["files"])
    assert receipt["files_installed"] and not receipt["activated"]
    assert not receipt["migrated"] and not receipt["execution_verified"]
    assert model["release"].is_dir() and not model["stage"].exists()
    assert policy.digest(model["release"] / "manifest.json") == model["expected"]


def test_staged_install_rechecks_final_payload_before_publication_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    original = installer.copy_verified

    def copy(source, target, expected, mode):
        original(source, target, expected, mode)
        if target == model["stage"] / "manifest.json":
            # Benign owned fixture bytes only; actual corruption/path safety
            # acceptance belongs to the independent VM, not this modeled role.
            leaf = model["stage"] / "payload/frontend/index.html"
            leaf.chmod(0o600)
            leaf.write_bytes(b"different ordinary fixture data\n")
            leaf.chmod(0o444)

    monkeypatch.setattr(installer, "copy_verified", copy)
    with pytest.raises(policy.DeliveryError, match="contents differ"):
        installer.files(model["bundle"], model["expected"])
    assert not model["release"].exists() and model["stage"].is_dir()
    assert not (
        model["root"] / "etc/kf-onlinejudge" / (model["data"]["release_id"] + ".files.json")
    ).exists()


def test_repeat_install_reads_each_GNU_image_once(ordinary_installation_model, monkeypatch):
    model = ordinary_installation_model
    installer.files(model["bundle"], model["expected"])
    root = model["images"] / "gnu"
    reads = []
    original = manifest._read_payload

    def read(row):
        if root in row[0].parents:
            reads.append(row[0].relative_to(root).as_posix())
        return original(row)

    monkeypatch.setattr(manifest, "_read_payload", read)
    installer.files(model["bundle"], model["expected"])
    expected = [
        row["path"].removeprefix("images/gnu/")
        for row in model["data"]["files"]
        if row["path"].startswith("images/gnu/")
    ]
    assert Counter(reads) == Counter(expected)


def test_repeat_install_still_refuses_legacy_byte_change_model(ordinary_installation_model):
    model = ordinary_installation_model
    installer.files(model["bundle"], model["expected"])
    leaf = model["images"] / "legacy-gnu/bin/ordinary-data"
    leaf.chmod(0o600)
    leaf.write_bytes(b"changed owned ordinary image fixture\n")
    leaf.chmod(0o444)
    with pytest.raises(policy.DeliveryError, match="legacy compatibility image bytes differ"):
        installer.files(model["bundle"], model["expected"])


def test_payload_byte_reads_are_bounded_joined_and_complete(ordinary_bundle, monkeypatch):
    # Ordinary owned bytes only. This observes the inventory's actual reads,
    # not service permissions, isolation, or VM recovery latency.
    bundle, _ = ordinary_bundle
    payload = bundle / "payload"
    expected = manifest.inventory(payload)
    original = manifest._read_payload
    release, saturated = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    started, results, errors = [], [], []

    def read(row):
        nonlocal active, maximum
        with lock:
            started.append(row[0])
            active += 1
            maximum = max(active, maximum)
            if active == 4:
                saturated.set()
        try:
            assert release.wait(5)
            return original(row)
        finally:
            with lock:
                active -= 1

    def collect():
        try:
            results.extend(manifest.inventory(payload))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(manifest, "_read_payload", read)
    collector = threading.Thread(target=collect)
    collector.start()
    try:
        assert saturated.wait(5)
        with lock:
            assert len(started) == maximum == 4
        assert collector.is_alive() and not results
    finally:
        release.set()
        collector.join(10)
    assert not collector.is_alive() and not errors and active == 0
    assert results == expected
    assert Counter(started) == Counter(payload / row["path"] for row in expected)


@pytest.mark.parametrize("index", range(9))
def test_payload_reader_refuses_identity_drift_model(ordinary_bundle, monkeypatch, index):
    bundle, _ = ordinary_bundle
    path = bundle / "payload/frontend/index.html"
    info = path.lstat()
    expected = list(manifest._file_stamp(info))
    expected[index] += 1
    with pytest.raises(policy.DeliveryError, match="changed before byte verification"):
        manifest._read_payload((path, tuple(expected), {"path": "frontend/index.html"}))


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
    assert Counter(reads) == Counter(first_reads * 2)


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
    assert Counter(reads) == Counter(first_reads * 2)


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
    assert Counter(reads) == Counter(complete_read)
    assert manifest._READ_SCOPE.get() is None
    assert not source.exists()
    manifest.verify(bundle, expected)
    assert Counter(reads) == Counter(complete_read * 2)


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
        assert Counter(reads) == Counter(first_reads * 2)


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
    assert Counter(reads) == Counter(first_reads * 2)


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


def test_setup_session_runs_real_cli_and_releases_complete_proofs(ordinary_bundle, monkeypatch):
    # Ordinary owned data and a modeled operator identity, not installation or
    # isolation acceptance. The real CLI and verifier still read fixture bytes.
    bundle, expected = ordinary_bundle
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    reads = observed_digests(monkeypatch)
    arguments = ["verify", str(bundle), "--manifest-sha256", expected]
    with setup_session.SetupSession(maximum_seconds=5) as operation:
        first = operation.invoke("scripts.release", arguments)
        initial = list(reads)
        second = operation.invoke("scripts.release", arguments)
        assert first.exit_code == second.exit_code == 0
        assert first.stdout == second.stdout and json.loads(first.stdout)["bundle_valid"]
        assert initial and reads == initial and not operation.completed
    assert operation.completed and len(operation.records) == 2
    assert manifest._READ_SCOPE.get() is None
    with setup_session.SetupSession(maximum_seconds=5) as another:
        another.invoke("scripts.release", arguments)
    assert Counter(reads) == Counter(initial * 2)
    with pytest.raises(policy.DeliveryError, match="single-use"):
        with operation:
            pass


def test_setup_session_does_not_remember_renamed_staging_tree(ordinary_bundle, monkeypatch):
    bundle, expected = ordinary_bundle
    reads = observed_digests(monkeypatch)
    published = bundle.with_name(bundle.name + "-published")
    with manifest.verification_scope():
        manifest.verify_staged(bundle, expected)
        first = list(reads)
        assert not manifest._READ_SCOPE.get().entries
        bundle.rename(published)
        manifest.verify(published, expected)
        assert len(reads) == 2 * len(first)
    assert manifest._READ_SCOPE.get() is None


def test_setup_session_checks_metadata_before_completing(ordinary_bundle, monkeypatch):
    bundle, expected = ordinary_bundle
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    signature = manifest._bundle_signature
    changed = False

    def drift(path):
        value = signature(path)
        if changed:
            stamp = list(value["manifest.json"])
            stamp[-1] += 1
            value["manifest.json"] = tuple(stamp)
        return value

    monkeypatch.setattr(manifest, "_bundle_signature", drift)
    operation = setup_session.SetupSession(maximum_seconds=5)
    with pytest.raises(policy.DeliveryError, match="verified bundle changed"):
        with operation:
            result = operation.invoke(
                "scripts.release", ["verify", str(bundle), "--manifest-sha256", expected]
            )
            assert result.exit_code == 0 and not operation.completed
            changed = True
    assert not operation.completed and manifest._READ_SCOPE.get() is None


def test_setup_session_keeps_cli_failure_and_stops_following_calls(monkeypatch):
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    operation = setup_session.SetupSession(maximum_seconds=5)
    with pytest.raises(policy.DeliveryError, match="failed setup session"):
        with operation:
            result = operation.invoke("scripts.release", ["verify"])
            assert result.exit_code == 2 and result.stderr
            with pytest.raises(policy.DeliveryError, match="active successful"):
                operation.invoke("scripts.release", ["verify"])
    assert not operation.completed and operation.records[0]["actual_exit_code"] == 2
    assert manifest._READ_SCOPE.get() is None


def test_setup_session_excludes_runtime_execution_and_inside_override(monkeypatch):
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    for module, argv in [
        ("scripts.release.lifecycle", ["--action", "start"]),
        ("scripts.release", ["build"]),
        ("scripts.release.restore", ["--inside"]),
        ("scripts.release.services", ["check-binding"]),
        ("scripts.release.journal", ["--help"]),
        ("scripts.release.dependencies", ["--check-service", "postgres"]),
        ("scripts.release.dependencies", ["--check-s=postgres"]),
        ("scripts.release.proxy_install", ["--check-binding"]),
        ("scripts.release.proxy_install", ["--check-b"]),
    ]:
        with pytest.raises(policy.DeliveryError):
            with setup_session.SetupSession(maximum_seconds=5) as operation:
                operation.invoke(module, argv)
        assert operation.records == [] and manifest._READ_SCOPE.get() is None


def test_setup_session_rejects_operator_identity_and_invalid_deadline(monkeypatch):
    monkeypatch.setattr(setup_session.os, "geteuid", lambda: 1001)
    with pytest.raises(policy.DeliveryError, match="trusted root operator"):
        with setup_session.SetupSession():
            pass
    for value in [False, 0, -1, 2401, float("inf"), float("nan"), "1800"]:
        with pytest.raises(policy.DeliveryError, match="finite setup deadline"):
            setup_session.SetupSession(maximum_seconds=value)
    assert manifest._READ_SCOPE.get() is None


def test_setup_session_preserves_existing_earlier_alarm(monkeypatch):
    import signal

    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    handler = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)

    def caller_deadline(*args):
        raise AssertionError("the earlier caller alarm must remain installed")

    try:
        signal.signal(signal.SIGALRM, caller_deadline)
        signal.setitimer(signal.ITIMER_REAL, 10)
        with setup_session.SetupSession(maximum_seconds=20) as operation:
            assert signal.getsignal(signal.SIGALRM) is caller_deadline
            assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 10
        assert operation.completed and signal.getsignal(signal.SIGALRM) is caller_deadline
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 10
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *timer)


def test_setup_session_own_deadline_interrupts_call_and_clears_scope(monkeypatch):
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    entry = importlib.import_module("scripts.release.__main__")

    def wait_for_deadline(argv):
        time.sleep(3)

    monkeypatch.setattr(entry, "main", wait_for_deadline)
    operation = setup_session.SetupSession(maximum_seconds=0.1)
    with pytest.raises(policy.DeliveryError, match="deadline exceeded"):
        with operation:
            operation.invoke("scripts.release", ["verify"])
    assert not operation.completed and manifest._READ_SCOPE.get() is None


def test_setup_session_call_count_is_finite(ordinary_bundle, monkeypatch):
    bundle, expected = ordinary_bundle
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    operation = setup_session.SetupSession(maximum_seconds=5)
    arguments = ["verify", str(bundle), "--manifest-sha256", expected]
    with pytest.raises(policy.DeliveryError, match="oversized setup"):
        with operation:
            for _ in range(32):
                assert operation.invoke("scripts.release", arguments).exit_code == 0
            operation.invoke("scripts.release", arguments)
    assert len(operation.records) == 32 and not operation.completed
    assert manifest._READ_SCOPE.get() is None


def test_setup_session_unexpected_exception_is_not_an_exit_success(monkeypatch):
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    entry = importlib.import_module("scripts.release.__main__")

    def refuse(argv):
        print("ordinary progress before refusal")
        raise OSError("ordinary call failure model")

    monkeypatch.setattr(entry, "main", refuse)
    operation = setup_session.SetupSession(maximum_seconds=5)
    with pytest.raises(OSError, match="ordinary call failure"):
        with operation:
            operation.invoke("scripts.release", ["verify"])
    assert operation.records[0]["actual_exit_code"] is None
    assert operation.records[0]["actual_exception_type"] == "OSError"
    assert operation.records[0]["stdout_bytes"] > 0 and not operation.completed
    assert manifest._READ_SCOPE.get() is None


def test_setup_session_output_is_bounded_in_bytes(monkeypatch):
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    entry = importlib.import_module("scripts.release.__main__")

    def oversized(argv):
        print("界" * 10923)

    monkeypatch.setattr(entry, "main", oversized)
    operation = setup_session.SetupSession(maximum_seconds=5)
    with pytest.raises(policy.DeliveryError, match="output exceeds bound"):
        with operation:
            operation.invoke("scripts.release", ["verify"])
    assert operation.records[0]["actual_exit_code"] is None
    assert not operation.completed and manifest._READ_SCOPE.get() is None


def test_setup_session_rejects_private_option_abbreviations(monkeypatch):
    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    for argument in ["--ins", "--inside=1", "--he", "--help=true", "-hfoo", "--"]:
        with pytest.raises(policy.DeliveryError, match="setup CLI call"):
            with setup_session.SetupSession(maximum_seconds=5) as operation:
                operation.invoke("scripts.release.restore", [argument])
        assert operation.records == [] and not operation.completed
        assert manifest._READ_SCOPE.get() is None


def test_setup_session_failed_alarm_installation_restores_handler(monkeypatch):
    import signal

    monkeypatch.setattr(setup_session, "_require_operator", lambda: None)
    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)
    real = signal.setitimer

    def refused_alarm(which, seconds, interval=0):
        if seconds:
            raise OSError("ordinary timer-installation refusal model")
        return real(which, seconds, interval)

    signal.setitimer(signal.ITIMER_REAL, 0)
    monkeypatch.setattr(setup_session.signal, "setitimer", refused_alarm)
    try:
        with pytest.raises(OSError, match="timer-installation refusal"):
            with setup_session.SetupSession():
                pass
        assert signal.getsignal(signal.SIGALRM) is original_handler
        assert manifest._READ_SCOPE.get() is None
    finally:
        monkeypatch.setattr(setup_session.signal, "setitimer", real)
        if original_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *original_timer)


@pytest.fixture
def ordinary_lifecycle_model(monkeypatch):
    # Every manager, clock, identity check and publication is modeled here.
    # Actual overlapping starts, limits, Root and cleanup remain VM-only.
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = {"release_id": "sd-" + "b" * 12, "configuration_id": "cfg-" + "c" * 24}
    maintenance.payload = Path("/ordinary-unused-payload")
    maintenance.record = {"events": [], "root_cleanup_confirmed": True}
    states = {name: "inactive" for name in (*lifecycle.DEPENDENCIES, *lifecycle.APPS)}
    trace, clock = [], [0.0]

    def unit(name):
        trace.append(("read", name))
        return {
            "ActiveState": states[name],
            "SubState": "running" if states[name] == "active" else "dead",
            "MainPID": "123" if states[name] == "active" else "0",
        }

    def dispatch(*args):
        assert args[:2] == ("--no-block", "start")
        name = args[2].removeprefix("kf-").removesuffix(".service")
        trace.append(("dispatch", name))
        states[name] = "active"

    monkeypatch.setattr(maintenance, "unit", unit)
    monkeypatch.setattr(maintenance, "save", lambda phase: trace.append(("save", phase)))
    monkeypatch.setattr(lifecycle.install, "selection", lambda **_: maintenance.selected)
    monkeypatch.setattr(lifecycle.service_install, "systemctl", dispatch)
    monkeypatch.setattr(
        lifecycle.node_health, "inspect_unit", lambda name, _: trace.append(("inspect", name))
    )
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lifecycle.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    return maintenance, states, trace, clock


def test_ordinary_start_groups_fit_parent_cpu_budget():
    # This checks scheduling policy; actual startup timing remains VM-only.
    node = Path(__file__).resolve().parents[2] / "deploy/release/kfonlinejudge.slice"
    quotas = [line for line in node.read_text().splitlines() if line.startswith("CPUQuota=")]
    assert len(quotas) == 1
    node_cpu = int(quotas[0].partition("=")[2].removesuffix("%"))
    profiles = lifecycle.services.PROFILES
    dispatched = [name for group in lifecycle.START_GROUPS for name in group if name in profiles]
    assert sorted(dispatched) == sorted(profiles)
    for group in lifecycle.START_GROUPS:
        assert sum(profiles[name][3] for name in group if name in profiles) <= node_cpu


def test_start_group_dispatches_independent_units_before_confirming_each(ordinary_lifecycle_model):
    maintenance, _, trace, _ = ordinary_lifecycle_model
    names = lifecycle.START_GROUPS[0]
    maintenance.start_group(names)
    starts = [(i, name) for i, (kind, name) in enumerate(trace) if kind == "dispatch"]
    inspections = [(i, name) for i, (kind, name) in enumerate(trace) if kind == "inspect"]
    assert [name for _, name in starts] == list(names)
    assert {name for _, name in inspections} == set(names)
    assert min(i for i, _ in inspections) > max(i for i, _ in starts)
    assert all(event["confirmed"] for event in maintenance.record["events"])
    assert maintenance.record["root_cleanup_confirmed"] is False


def test_start_group_validates_all_targets_before_any_dispatch(
    ordinary_lifecycle_model, monkeypatch
):
    maintenance, _, trace, _ = ordinary_lifecycle_model
    original = maintenance.unit

    def unknown(name):
        if name == "rabbitmq":
            raise policy.DeliveryError("ordinary unknown target model")
        return original(name)

    monkeypatch.setattr(maintenance, "unit", unknown)
    with pytest.raises(policy.DeliveryError, match="unknown target"):
        maintenance.start_group(lifecycle.START_GROUPS[0])
    assert not any(kind == "dispatch" for kind, _ in trace)
    assert maintenance.record["events"] == []


def test_later_dispatch_does_not_reset_an_earlier_start_deadline(
    ordinary_lifecycle_model, monkeypatch
):
    maintenance, _, trace, clock = ordinary_lifecycle_model
    original = lifecycle.service_install.systemctl

    def delayed(*args):
        original(*args)
        if args[-1] == "kf-rabbitmq.service":
            clock[0] = 131

    monkeypatch.setattr(lifecycle.service_install, "systemctl", delayed)
    with pytest.raises(policy.DeliveryError, match="bounded lifecycle"):
        maintenance.start_group(lifecycle.START_GROUPS[0])
    assert [name for kind, name in trace if kind == "dispatch"] == ["postgres", "rabbitmq"]
    assert all(not event["confirmed"] for event in maintenance.record["events"])


def test_start_group_refuses_changed_selection_before_dispatch(
    ordinary_lifecycle_model, monkeypatch
):
    maintenance, _, trace, _ = ordinary_lifecycle_model
    monkeypatch.setattr(lifecycle.install, "selection", lambda **_: {})
    with pytest.raises(policy.DeliveryError, match="selected binding changed"):
        maintenance.start_group(lifecycle.START_GROUPS[0])
    assert not any(kind == "dispatch" for kind, _ in trace)


def test_start_group_resume_checks_live_profiles_without_duplicate_starts(ordinary_lifecycle_model):
    maintenance, states, trace, _ = ordinary_lifecycle_model
    group = lifecycle.START_GROUPS[0]
    for name in group:
        states[name] = "active"
    maintenance.start_group(group)
    assert {name for kind, name in trace if kind == "inspect"} == set(group)
    assert not any(kind == "dispatch" for kind, _ in trace)
    assert maintenance.record["events"] == []


def test_group_profile_failure_cannot_confirm_a_started_unit(ordinary_lifecycle_model, monkeypatch):
    maintenance, _, trace, _ = ordinary_lifecycle_model

    def refused(name, selected):
        raise policy.DeliveryError("ordinary effective profile refusal model")

    monkeypatch.setattr(lifecycle.node_health, "inspect_unit", refused)
    with pytest.raises(policy.DeliveryError, match="effective profile refusal"):
        maintenance.start_group(lifecycle.START_GROUPS[0])
    assert len([name for kind, name in trace if kind == "dispatch"]) == 3
    assert all(not event["confirmed"] for event in maintenance.record["events"])


def test_startup_dependency_barriers_precede_both_real_readiness_calls(
    ordinary_lifecycle_model, monkeypatch
):
    maintenance, states, trace, _ = ordinary_lifecycle_model
    gate, observations = [], []
    monkeypatch.setattr(lifecycle.availability, "write", lambda _, state: gate.append(state))

    def observe(*args):
        verified = {name for kind, name in trace if kind == "inspect"}
        assert verified == set(states) and all(value == "active" for value in states.values())
        observations.append(gate[-1])
        return {"service_preconditions_met": True, "ready_for_admission": gate[-1] == "open"}

    monkeypatch.setattr(lifecycle.operator_units, "run", observe)
    maintenance.start_locked(resume=True)
    assert gate == observations == ["draining", "open"]
    dispatches = [name for kind, name in trace if kind == "dispatch"]
    assert dispatches == [*lifecycle.DEPENDENCIES, *lifecycle.START_ORDER]
    for dependency in (*lifecycle.DEPENDENCIES, "sandbox-factory"):
        assert trace.index(("inspect", dependency)) < trace.index(("dispatch", "coordinator"))
    assert trace.index(("inspect", "api")) < trace.index(("dispatch", "proxy"))


def test_startup_group_failure_keeps_admission_closed(ordinary_lifecycle_model, monkeypatch):
    maintenance, states, trace, _ = ordinary_lifecycle_model
    gate = []
    monkeypatch.setattr(lifecycle.availability, "write", lambda _, state: gate.append(state))
    original = lifecycle.service_install.systemctl

    def failing(*args):
        original(*args)
        states["rabbitmq"] = "failed"

    monkeypatch.setattr(lifecycle.service_install, "systemctl", failing)
    monkeypatch.setattr(
        lifecycle.operator_units,
        "run",
        lambda *_: pytest.fail("readiness cannot follow failed stage"),
    )
    with pytest.raises(policy.DeliveryError, match="bounded lifecycle"):
        maintenance.start_locked(resume=True)
    assert gate == ["draining"]
    assert not any(
        kind == "dispatch" and name in lifecycle.services.PROFILES for kind, name in trace
    )


@pytest.mark.parametrize("names", [(), ("api",), ("postgres", "postgres")])
def test_startup_rejects_unknown_or_duplicate_groups(ordinary_lifecycle_model, names):
    maintenance, _, trace, _ = ordinary_lifecycle_model
    with pytest.raises(policy.DeliveryError, match="unknown startup dependency group"):
        maintenance.start_group(names)
    assert trace == []
