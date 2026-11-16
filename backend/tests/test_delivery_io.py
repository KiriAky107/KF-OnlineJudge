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
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

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
operation_proof = importlib.import_module("scripts.release.operation_proof")


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


@pytest.mark.parametrize("missing", ["frontend", "frontend/admin", "lib", "lib/share"])
def test_manifest_requires_every_file_and_empty_directory_ancestor(ordinary_bundle, missing):
    bundle, _ = ordinary_bundle
    data = json.loads((bundle / "manifest.json").read_bytes())
    data["directories"].extend(["lib", "lib/share", "lib/share/locale"])
    assert manifest.validate(data) is data

    data["directories"].remove(missing)
    with pytest.raises(policy.DeliveryError, match="missing payload parent directory"):
        manifest.validate(data)


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


def test_protected_tree_keeps_complete_coverage_with_shared_ancestor_bound_model(
    tmp_path, monkeypatch
):
    root = tmp_path / "ordinary-protected-tree-model"
    folders = ["lib", "lib/nested", "lib/nested/deep", "lib/empty"]
    files = [f"lib/nested/deep/item-{index}" for index in range(64)]
    observed = []

    def protected(path, *, directory=False, ancestors=True):
        observed.append((path, directory, ancestors))

    monkeypatch.setattr(installer.preflight, "protected", protected)
    installer.preflight.protected_tree(root, files, folders)
    assert {path for path, _, _ in observed} == {
        root,
        *(root / name for name in files + folders),
    }
    assert [(path, directory) for path, directory, ancestors in observed if ancestors] == [
        (root, True),
        (root, True),
    ]
    assert len(observed) == len(files) + len(folders) + 2
    assert (
        observed.index((root / "lib/nested", True, False))
        < observed.index((root / "lib/nested/deep", True, False))
        < observed.index((root / files[0], False, False))
    )
    assert (root / "lib/empty", True, False) in observed


def test_protected_tree_requires_closed_unambiguous_inventory_before_lookup_model(
    tmp_path, monkeypatch
):
    observed = []
    monkeypatch.setattr(installer.preflight, "protected", lambda *a, **k: observed.append(a))
    for files, folders in (
        (["lib/missing/leaf"], []),
        (["lib/nested/deep/leaf"], ["lib/nested/deep"]),
        (["lib/same", "lib/same"], ["lib"]),
        ([], ["lib", "lib", "lib/same"]),
        (["lib/same"], ["lib", "lib/same"]),
    ):
        with pytest.raises(policy.DeliveryError, match="protected tree"):
            installer.preflight.protected_tree(tmp_path, files, folders)
        assert observed == []


def test_protected_tree_rechecks_owner_refusal_on_each_call_model(tmp_path, monkeypatch):
    root = tmp_path / "ordinary-protected-tree-model"
    refused = set()
    visited = []

    def protected(path, **_):
        visited.append(path)
        if path in refused:
            raise policy.DeliveryError("ordinary modeled ownership refusal")

    monkeypatch.setattr(installer.preflight, "protected", protected)
    installer.preflight.protected_tree(
        root, ["lib/nested/leaf"], ["lib", "lib/nested", "lib/empty"]
    )
    refused.add(root / "lib/empty")
    visited.clear()
    with pytest.raises(policy.DeliveryError, match="modeled ownership refusal"):
        installer.preflight.protected_tree(
            root, ["lib/nested/leaf"], ["lib", "lib/nested", "lib/empty"]
        )
    assert root / "lib/empty" in visited


def test_single_file_copy_retains_full_ancestor_checks_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    source = model["bundle"] / "payload/backend/alembic.ini"
    target = model["root"] / "ordinary-single-file"
    observed = []

    def protected(path, *, directory=False, ancestors=True):
        observed.append((path, directory, ancestors))

    monkeypatch.setattr(installer.preflight, "protected", protected)
    installer.copy_verified(source, target, policy.digest(source), 0o444)
    assert target.read_bytes() == source.read_bytes()
    assert observed and all(ancestors for _, _, ancestors in observed)
    assert (source, False, True) in observed
    assert (target.parent, True, True) in observed


def test_new_tree_owner_change_model_refuses_before_publication(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    source = model["bundle"] / "payload"
    changed = False
    original = installer._copy_verified

    def copy(path, target, *args, **kwargs):
        nonlocal changed
        result = original(path, target, *args, **kwargs)
        if path == source / "backend/alembic.ini":
            changed = True
        return result

    def protected(path, *, directory=False, ancestors=True):
        if changed and directory and path == source / "backend":
            raise policy.DeliveryError("ordinary modeled directory owner changed")

    monkeypatch.setattr(installer.preflight, "protected", protected)
    monkeypatch.setattr(installer, "_copy_verified", copy)
    with pytest.raises(policy.DeliveryError, match="modeled directory owner changed"):
        installer.files(model["bundle"], model["expected"])
    assert changed and model["stage"].is_dir() and not model["release"].exists()


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


def installation_space_model(model, monkeypatch):
    # Existing ordinary fixtures model Root ownership; only their real bytes,
    # inventories and modes are exercised here. Actual capacity belongs to VM acceptance.
    monkeypatch.setattr(installer.preflight, "RELEASES", model["release"].parent)
    return installer.preflight.installation_space(
        model["bundle"] / "payload", bundle_data=model["data"]
    )


def test_fresh_install_keeps_full_payload_images_and_data_reserve_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    space = installation_space_model(model, monkeypatch)
    assert space["new_payload_bytes"] == model["data"]["payload_bytes"]
    assert space["verified_existing_payload_bytes"] == 0
    assert space["mutable_operation_reserve_bytes"] == 4 * 1024**3
    assert space["required_free_bytes"] == sum(
        space[key]
        for key in (
            "new_payload_bytes",
            "protected_image_reserve_bytes",
            "legacy_image_reserve_bytes",
            "mutable_operation_reserve_bytes",
        )
    )


def test_repeat_install_proves_payload_once_and_keeps_image_and_data_reserves_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    fresh = installation_space_model(model, monkeypatch)
    first = installer.files(model["bundle"], model["expected"])
    free = fresh["required_free_bytes"] - 1
    spaces = []
    reads = []
    original = manifest._read_payload

    def read(row):
        if model["release"] in row[0].parents:
            reads.append(row[0].relative_to(model["release"] / "payload").as_posix())
        return original(row)

    def eligible(payload, *, bundle_data):
        space = installer.preflight.installation_space(payload, bundle_data=bundle_data)
        spaces.append(space)
        return {"eligible": space["required_free_bytes"] <= free, "errors": []}

    monkeypatch.setattr(manifest, "_read_payload", read)
    monkeypatch.setattr(installer.preflight, "observe", eligible)
    assert installer.files(model["bundle"], model["expected"]) == first
    assert Counter(reads) == Counter(row["path"] for row in model["data"]["files"])
    assert len(spaces) == 1
    assert spaces[0]["new_payload_bytes"] == 0
    assert spaces[0]["verified_existing_payload_bytes"] == model["data"]["payload_bytes"]
    assert (
        fresh["required_free_bytes"] - spaces[0]["required_free_bytes"]
        == model["data"]["payload_bytes"]
    )
    for key in (
        "protected_image_reserve_bytes",
        "legacy_image_reserve_bytes",
        "mutable_operation_reserve_bytes",
    ):
        assert spaces[0][key] == fresh[key]
    assert manifest._READ_SCOPE.get() is None


def test_repeat_install_below_unchanged_reserve_still_refuses_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    installer.files(model["bundle"], model["expected"])
    repeat = installation_space_model(model, monkeypatch)
    before = manifest._bundle_signature(model["release"])
    free = repeat["required_free_bytes"] - 1

    def eligible(payload, *, bundle_data):
        space = installer.preflight.installation_space(payload, bundle_data=bundle_data)
        return {"eligible": space["required_free_bytes"] <= free, "errors": ["capacity"]}

    monkeypatch.setattr(installer.preflight, "observe", eligible)
    with pytest.raises(policy.DeliveryError, match="installation preflight refused: capacity"):
        installer.files(model["bundle"], model["expected"])
    assert manifest._bundle_signature(model["release"]) == before


def test_changed_installed_payload_is_not_counted_as_available_space_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    installer.files(model["bundle"], model["expected"])
    leaf = model["release"] / "payload/frontend/index.html"
    leaf.chmod(0o600)
    leaf.write_bytes(b"different ordinary fixture bytes\n")
    leaf.chmod(0o444)
    with pytest.raises(policy.DeliveryError, match="contents differ"):
        installation_space_model(model, monkeypatch)
    assert leaf.read_bytes() == b"different ordinary fixture bytes\n"


def test_existing_release_seal_is_not_repaired_for_space_accounting_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    installer.files(model["bundle"], model["expected"])
    model["release"].chmod(0o700)
    with pytest.raises(policy.DeliveryError, match="existing release seal differs"):
        installation_space_model(model, monkeypatch)
    assert model["release"].stat().st_mode & 0o777 == 0o700


def test_unpublished_stage_does_not_reduce_fresh_install_reserve_model(
    ordinary_installation_model, monkeypatch
):
    model = ordinary_installation_model
    before = installation_space_model(model, monkeypatch)
    model["stage"].parent.mkdir(parents=True, exist_ok=True)
    model["stage"].mkdir(mode=0o700)
    assert installation_space_model(model, monkeypatch) == before
    with pytest.raises(policy.DeliveryError, match="incomplete release"):
        installer.files(model["bundle"], model["expected"])
    assert model["stage"].is_dir() and not model["release"].exists()


@pytest.mark.parametrize("root_form", ["absolute", "relative", "dot", "parent"])
@pytest.mark.parametrize("path_prefix", ["", "site/"])
def test_inventory_keeps_nested_names_for_each_root_form(
    tmp_path, monkeypatch, root_form, path_prefix
):
    # Ordinary owned fixture bytes only, never runtime or isolation acceptance.
    payload = tmp_path / "ordinary-root"
    contents = {
        "frontend/top.txt": b"ordinary trusted inventory bytes\n",
        "frontend/branch/leaf.txt": b"another ordinary file\n",
        "frontend/branch/ordinary-root/leaf.txt": b"nested root name remains a member\n",
    }
    for name, data in contents.items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o555 if name == "frontend/top.txt" else 0o444)
    (payload / "frontend/branch/empty").mkdir()
    for path in payload.rglob("*"):
        if path.is_dir():
            path.chmod(0o555)
    payload.chmod(0o555)
    monkeypatch.chdir(payload if root_form == "dot" else tmp_path)
    root = {
        "absolute": payload,
        "relative": Path("ordinary-root"),
        "dot": Path("."),
        "parent": Path("ordinary-root/../ordinary-root"),
    }[root_form]
    expected = [
        {
            "path": name,
            "bytes": len(data),
            "mode": "0555" if name == "frontend/top.txt" else "0444",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for name, data in sorted(contents.items())
    ]
    assert manifest.inventory(root, path_prefix=path_prefix) == expected
    assert manifest.directories(root, path_prefix=path_prefix) == [
        "frontend",
        "frontend/branch",
        "frontend/branch/empty",
        "frontend/branch/ordinary-root",
    ]


@pytest.mark.parametrize("operation", ["inventory", "directories"])
def test_payload_walk_refuses_a_sibling_root_record(tmp_path, monkeypatch, operation):
    # Synthetic walker record only; no file outside the owned root is accessed.
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        manifest.os,
        "walk",
        lambda *_args, **_kwargs: iter([(str(tmp_path / "root-sibling"), [], ["file.txt"])]),
    )
    with pytest.raises(policy.DeliveryError, match="payload walk escaped its root"):
        getattr(manifest, operation)(root)


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
    started, readers, results, errors = [], [], [], []

    def read(row):
        nonlocal active, maximum
        with lock:
            started.append(row[0])
            readers.append(threading.get_ident())
            active += 1
            maximum = max(active, maximum)
            if active == 1:
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
            assert len(started) == maximum == 1
        assert collector.is_alive() and not results
    finally:
        release.set()
        collector.join(10)
    assert not collector.is_alive() and not errors and active == 0
    assert results == expected
    assert Counter(started) == Counter(payload / row["path"] for row in expected)
    assert readers == [collector.ident] * len(expected)


@pytest.mark.parametrize("index", range(9))
def test_payload_reader_refuses_identity_drift_model(ordinary_bundle, monkeypatch, index):
    bundle, _ = ordinary_bundle
    path = bundle / "payload/frontend/index.html"
    info = path.lstat()
    expected = list(manifest._file_stamp(info))
    expected[index] += 1
    with pytest.raises(policy.DeliveryError, match="changed before byte verification"):
        manifest._read_payload((path, tuple(expected), {"path": "frontend/index.html"}, None))


@pytest.mark.parametrize("source_present,mode", [(False, "0444"), (True, "0555")])
def test_manifest_rejects_orphan_or_executable_bytecode(ordinary_bundle, source_present, mode):
    bundle, _ = ordinary_bundle
    data = json.loads((bundle / "manifest.json").read_bytes())
    if source_present:
        data["files"].append(
            {"path": "site/toy.py", "bytes": 0, "mode": "0444", "sha256": "b" * 64}
        )
    data["files"].append(
        {
            "path": "site/__pycache__/toy.cpython-313.pyc",
            "bytes": 0,
            "mode": mode,
            "sha256": "b" * 64,
        }
    )
    with pytest.raises(policy.DeliveryError, match="bytecode requires its inventoried source"):
        manifest.validate(data)


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


@pytest.fixture
def live_byte_proof_model(ordinary_bundle, monkeypatch, tmp_path):
    """Only modeled Root/manager ports; fixture bytes are never executable."""
    bundle, expected = ordinary_bundle
    data = manifest.verify(bundle, expected)
    record = {
        "schema_version": 1,
        "bundle": str(bundle),
        "manifest_sha256": expected,
        "release_id": data["release_id"],
        "configuration_id": "cfg-" + "1" * 24,
        "metadata_sha256": operation_proof._signature(manifest._bundle_signature(bundle)),
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "issued_boottime_ns": 1000,
        "pid": 7,
        "invocation": "1" * 32,
    }
    path = tmp_path / "byte-proof.json"
    path.write_bytes(policy.canonical(record))
    monkeypatch.setattr(operation_proof, "RECORD", path)
    for name in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(operation_proof.os, name, lambda: 0)
    monkeypatch.setattr(operation_proof.os, "getgroups", lambda: [0])
    monkeypatch.setattr(operation_proof.preflight, "protected", lambda *a, **kw: None)
    monkeypatch.setattr(
        operation_proof.preflight,
        "current_cgroup",
        lambda: Path(operation_proof.GROUP + "kf-outbox.service"),
    )
    monkeypatch.setattr(operation_proof, "_read", lambda: (dict(record), (8, 9)))
    monkeypatch.setattr(operation_proof, "_now", lambda: 1001)
    monkeypatch.setattr(operation_proof, "_boot", lambda: record["boot_id"])
    monkeypatch.setattr(
        operation_proof, "_manager", lambda *args: {"pid": 7, "invocation": "1" * 32}
    )
    return bundle, expected, record


def test_live_parent_model_reuses_complete_proof_without_remembering_it(
    live_byte_proof_model, monkeypatch
):
    bundle, expected, record = live_byte_proof_model
    reads = observed_digests(monkeypatch)
    with manifest.verification_scope():
        data = manifest.verify(bundle, expected)
        assert data["release_id"] == record["release_id"]
        assert not manifest._READ_SCOPE.get().entries
        monkeypatch.setattr(operation_proof, "_now", lambda: 1000 + operation_proof.MAX_AGE_NS + 1)
        with pytest.raises(policy.DeliveryError, match="expired or owner changed"):
            manifest.verify(bundle, expected)
    assert not reads


@pytest.mark.parametrize(
    ("unit", "gid"),
    [
        ("postgres", 1203),
        ("rabbitmq", 1204),
        ("sandbox-factory", 0),
        ("coordinator", 1201),
        ("api", 1200),
        ("outbox", 1200),
        ("problem-import", 1200),
        ("worker", 1202),
        ("proxy", 1205),
        ("node-health", 0),
    ],
)
def test_privileged_service_group_model_can_consume_but_not_publish(
    live_byte_proof_model, monkeypatch, unit, gid
):
    bundle, expected, record = live_byte_proof_model
    monkeypatch.setattr(operation_proof.os, "getgroups", lambda: [gid])
    monkeypatch.setattr(
        operation_proof.preflight,
        "current_cgroup",
        lambda: Path(operation_proof.GROUP + "kf-" + unit + ".service"),
    )
    reads = observed_digests(monkeypatch)
    assert manifest.verify(bundle, expected)["release_id"] == record["release_id"]
    assert not reads
    # Publication still requires Root's own groups, even in a consumer model.
    assert operation_proof._root() is (gid == 0)


@pytest.mark.parametrize(
    ("unit", "groups", "ordinary_id"),
    [
        ("api", [1201], None),
        ("worker", [1200], None),
        ("node-health", [1200], None),
        ("api", [0, 1200, 1201], None),
        ("api/child", [1200], None),
        ("api", [1200], "getuid"),
        ("api", [1200], "geteuid"),
        ("api", [1200], "getgid"),
        ("api", [1200], "getegid"),
    ],
)
def test_foreign_groups_and_each_ordinary_identity_model_require_full_bytes(
    live_byte_proof_model, monkeypatch, unit, groups, ordinary_id
):
    bundle, expected, _ = live_byte_proof_model
    monkeypatch.setattr(operation_proof.os, "getgroups", lambda: groups)
    if ordinary_id:
        monkeypatch.setattr(operation_proof.os, ordinary_id, lambda: 1200)
    monkeypatch.setattr(
        operation_proof.preflight,
        "current_cgroup",
        lambda: Path(operation_proof.GROUP + "kf-" + unit + ".service"),
    )
    reads = observed_digests(monkeypatch)
    manifest.verify(bundle, expected)
    assert reads


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("pid", True),
        ("pid", 0),
        ("issued_boottime_ns", False),
        ("issued_boottime_ns", -1),
        ("invocation", "unknown"),
        ("release_id", "../release"),
        ("configuration_id", None),
        ("manifest_sha256", "a" * 64),
        ("metadata_sha256", "0" * 64),
        ("boot_id", "unknown"),
    ],
)
def test_byte_proof_record_model_refuses_invalid_identity(live_byte_proof_model, field, value):
    _, _, record = live_byte_proof_model
    assert operation_proof.validate(record) == record
    with pytest.raises(policy.DeliveryError):
        operation_proof.validate({**record, field: value})
    with pytest.raises(policy.DeliveryError, match="fields"):
        operation_proof.validate({**record, "unknown": 1})


def test_byte_proof_model_checks_metadata_and_live_parent_again_before_return(
    live_byte_proof_model, monkeypatch
):
    bundle, expected, record = live_byte_proof_model
    calls = 0

    def manager(*args):
        nonlocal calls
        calls += 1
        return {"pid": record["pid"] if calls == 1 else 99, "invocation": record["invocation"]}

    monkeypatch.setattr(operation_proof, "_manager", manager)
    with pytest.raises(policy.DeliveryError, match="expired or owner changed"):
        manifest.verify(bundle, expected)
    assert calls == 2


@pytest.mark.parametrize("reason", ["ordinary_uid", "foreign_group", "parent", "absent"])
def test_ineligible_parent_proof_model_requires_full_bytes(
    live_byte_proof_model, monkeypatch, reason
):
    bundle, expected, _ = live_byte_proof_model
    if reason == "ordinary_uid":
        monkeypatch.setattr(operation_proof, "_root", lambda *args: False)
    elif reason in {"foreign_group", "parent"}:
        unit = "unrelated.service" if reason == "foreign_group" else operation_proof.PARENT
        monkeypatch.setattr(
            operation_proof.preflight,
            "current_cgroup",
            lambda: Path(operation_proof.GROUP + unit),
        )
    else:
        monkeypatch.setattr(operation_proof, "RECORD", bundle / "absent-proof")
    reads = observed_digests(monkeypatch)
    manifest.verify(bundle, expected)
    assert reads


@pytest.mark.parametrize("index", range(9))
def test_inherited_proof_model_refuses_every_metadata_change(
    live_byte_proof_model, monkeypatch, index
):
    bundle, expected, _ = live_byte_proof_model
    original = manifest._bundle_signature

    def changed(path):
        value = original(path)
        stamp = list(value["payload/bin/kf-worker"])
        stamp[index] += 1
        value["payload/bin/kf-worker"] = tuple(stamp)
        return value

    monkeypatch.setattr(manifest, "_bundle_signature", changed)
    with pytest.raises(policy.DeliveryError, match="metadata changed"):
        manifest.verify(bundle, expected)


@pytest.mark.parametrize("change", ["pid", "invocation", "bundle", "manifest", "future", "boot"])
def test_inherited_proof_model_refuses_foreign_owner_and_binding(
    live_byte_proof_model, monkeypatch, change
):
    bundle, expected, record = live_byte_proof_model
    if change == "pid":
        record["pid"] = 8
    elif change == "invocation":
        record["invocation"] = "2" * 32
    elif change == "bundle":
        record["bundle"] = "/different/bundle"
    elif change == "manifest":
        record["manifest_sha256"] = "2" * 64
    elif change == "future":
        record["issued_boottime_ns"] = 1002
    else:
        monkeypatch.setattr(operation_proof, "_boot", lambda: "another boot")
    reads = observed_digests(monkeypatch)
    with pytest.raises(policy.DeliveryError, match="bundle differs|expired or owner changed"):
        manifest.verify(bundle, expected)
    assert not reads  # Refusal must not quietly fall back around a foreign proof.


@pytest.mark.parametrize("failure", ["before_start", "after_readiness", "cleanup"])
def test_final_byte_proof_failure_model_closes_admission(monkeypatch, failure):
    recovery_database = importlib.import_module("scripts.release.recovery_database")
    monkeypatch.setattr(recovery_database, "admission_guard", nullcontext)
    events = []
    monkeypatch.setattr(
        lifecycle.availability,
        "write",
        lambda selected, state: events.append("admission_" + state),
    )

    @contextmanager
    def proof(*args):
        assert events == ["admission_draining"]
        if failure == "before_start":
            raise policy.DeliveryError("modeled proof unavailable")
        yield
        raise policy.DeliveryError("modeled final proof or cleanup refusal")

    monkeypatch.setattr(operation_proof, "publish", proof)
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = {}
    maintenance.payload = Path("/unexecuted/fixture/payload")
    maintenance.start_locked = lambda **kwargs: events.append("readiness_opened")
    maintenance.close_failed_admission = lambda: events.append("admission_closed")
    with pytest.raises(policy.DeliveryError, match="modeled"):
        maintenance.start(resume=True)
    assert events == ["admission_draining"] + (
        ["admission_closed"]
        if failure == "before_start"
        else ["readiness_opened", "admission_closed"]
    )


def test_start_on_active_node_model_refuses_before_proof_or_admission_changes(monkeypatch):
    recovery_database = importlib.import_module("scripts.release.recovery_database")
    monkeypatch.setattr(recovery_database, "admission_guard", nullcontext)
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.unit = lambda _: {"ActiveState": "active", "MainPID": "7"}
    maintenance.units = lambda names: {name: maintenance.unit(name) for name in names}
    calls = []
    monkeypatch.setattr(operation_proof, "publish", lambda *args: calls.append("proof"))
    maintenance.close_failed_admission = lambda: calls.append("admission")
    with pytest.raises(policy.DeliveryError, match="fresh start requires stopped"):
        maintenance.start(resume=False)
    assert not calls


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


@pytest.mark.parametrize("max_workers", [None, 2])
def test_copy_inventory_prefetch_is_bounded_and_results_keep_input_order(max_workers):
    # Trusted callbacks only: ordinary concurrency control, no VM/Root acceptance.
    limit = 4 if max_workers is None else max_workers
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
            if active == limit:
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
            results.extend(file_copy.bounded_copies(Inventory(), copy, max_workers=max_workers))
        except BaseException as error:
            errors.append(error)

    collector = threading.Thread(target=collect)
    collector.start()
    try:
        assert saturated.wait(5)
        with lock:
            assert consumed == list(range(limit))
            assert maximum == limit
    finally:
        release.set()
        collector.join(10)
    assert not collector.is_alive() and not errors
    assert results == [index * 3 for index in range(32)]
    assert active == 0 and maximum == limit


@pytest.mark.parametrize("max_workers", [None, 2])
def test_copy_failure_joins_dispatched_calls_before_propagating(max_workers):
    # No files/privileges: observe failure quiescence of trusted callbacks.
    limit = 4 if max_workers is None else max_workers
    together = threading.Barrier(limit)
    release, failing, returned = (threading.Event() for _ in range(3))
    lock = threading.Lock()
    active = 0
    errors, active_when_returned = [], []

    def copy(index):
        nonlocal active
        with lock:
            active += 1
        try:
            if index < limit:
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
            file_copy.bounded_copies(list(range(32)), copy, max_workers=max_workers)
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


@pytest.mark.parametrize("max_workers", [0, 5, True, 1.5])
def test_copy_worker_bound_refuses_before_dispatch(max_workers):
    calls = []
    with pytest.raises(policy.DeliveryError, match="concurrency exceeds fixed bound"):
        file_copy.bounded_copies(list(range(8)), calls.append, max_workers=max_workers)
    assert calls == []


def test_single_reader_uses_caller_thread_without_prefetching_inventory():
    owner = threading.get_ident()
    consumed, completed, threads = [], [], []

    class Inventory(Sequence):
        def __len__(self):
            return 32

        def __getitem__(self, index):
            if not 0 <= index < len(self):
                raise IndexError
            assert index == len(completed)
            consumed.append(index)
            return index

    def read(index):
        threads.append(threading.get_ident())
        completed.append(index)
        return index * 3

    assert file_copy.bounded_copies(Inventory(), read, max_workers=1) == [
        index * 3 for index in range(32)
    ]
    assert consumed == completed == list(range(32))
    assert threads == [owner] * 32


@pytest.mark.parametrize("failed_index", [0, 5, 31])
def test_single_reader_failure_propagates_without_later_dispatch(failed_index):
    calls = []
    refusal = ValueError("ordinary callback refusal model")

    def read(index):
        calls.append(index)
        if index == failed_index:
            raise refusal
        return index

    with pytest.raises(ValueError) as error:
        file_copy.bounded_copies(list(range(32)), read, max_workers=1)
    assert error.value is refusal
    assert calls == list(range(failed_index + 1))


def test_single_reader_deadline_refuses_before_consuming_another_item(monkeypatch):
    # Deterministic ordinary deadline control model; no real VM timing claim.
    clock = [0.0]
    monkeypatch.setattr(file_copy, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    consumed, calls = [], []

    class Inventory(Sequence):
        def __len__(self):
            return 32

        def __getitem__(self, index):
            if not 0 <= index < len(self):
                raise IndexError
            consumed.append(index)
            return index

    def read(index):
        calls.append(index)
        clock[0] = 2.0
        return index

    with pytest.raises(policy.DeliveryError, match="copy deadline exceeded"):
        file_copy.bounded_copies(Inventory(), read, max_workers=1, deadline=1.0)
    assert consumed == calls == [0]


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
    monkeypatch.setattr(
        maintenance, "units", lambda names: {name: maintenance.unit(name) for name in names}
    )
    monkeypatch.setattr(maintenance, "save", lambda phase: trace.append(("save", phase)))
    monkeypatch.setattr(lifecycle.install, "selection", lambda **_: maintenance.selected)
    monkeypatch.setattr(lifecycle.service_install, "systemctl", dispatch)
    monkeypatch.setattr(
        lifecycle.node_health, "inspect_unit", lambda name, _: trace.append(("inspect", name))
    )
    # This fixture remains an ordinary model; actual TLS/service readiness is VM-only.
    monkeypatch.setattr(
        lifecycle.proxy_install,
        "check_binding",
        lambda *_: trace.append(("proxy-binding", "proxy")),
    )

    def https_ready(selected, *, timeout):
        assert selected == maintenance.selected and 0 < timeout <= 5
        assert states["proxy"] == "active"
        trace.append(("https", "proxy"))
        return {
            "reachable": True,
            "pinned_certificate_and_hostname_verified": True,
            "public_browser_trust_verified": False,
        }

    monkeypatch.setattr(lifecycle.proxy_health, "observe", https_ready)
    monkeypatch.setattr(
        maintenance,
        "_wait_for_factory",
        lambda deadline, main_pid: trace.append(("factory-status", "sandbox-factory")),
    )

    def api_ready(url, *, timeout):
        assert url == "http://127.0.0.1:8000/api/v1/health/live" and 0 < timeout <= 5
        assert states["api"] == "active"
        trace.append(("api-live", "api"))
        return {"status": "ok", "service": "platform"}

    monkeypatch.setattr(lifecycle.node_health, "http", api_ready)
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        lifecycle.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    return maintenance, states, trace, clock


def test_full_bundle_start_groups_fit_parent_cpu_budget():
    # Render the real dependency and Root ceilings too. This is a scheduling
    # invariant, not an actual service, role, timing or VM acceptance result.
    node = Path(__file__).resolve().parents[2] / "deploy/release/kfonlinejudge.slice"
    quotas = [line for line in node.read_text().splitlines() if line.startswith("CPUQuota=")]
    assert len(quotas) == 1
    node_cpu = int(quotas[0].partition("=")[2].removesuffix("%"))
    profiles = lifecycle.services.PROFILES
    budgets = {name: values[3] for name, values in profiles.items()}

    def rendered_cpu(contents):
        lines = [line for line in contents.decode().splitlines() if line.startswith("CPUQuota=")]
        assert len(lines) == 1
        return int(lines[0].partition("=")[2].removesuffix("%"))

    dependencies = importlib.import_module("scripts.release.dependencies")
    configuration = factory_service.configuration
    source, expected = "b" * 40, "c" * 64
    hashes = dict.fromkeys(configuration.CONFIG_FILES, "d" * 64)
    receipt = {
        "schema_version": 1,
        "source_sha": source,
        "release_id": "sd-" + source[:12],
        "configuration_id": configuration.configuration_id(source, expected, hashes),
        "manifest_sha256": expected,
        "configuration_files": hashes,
        "operator_attested_measurement": False,
        "installed": False,
        "execution_verified": False,
    }
    units = dependencies.render(receipt["release_id"], receipt["configuration_id"])
    for name in lifecycle.DEPENDENCIES:
        budgets[name] = rendered_cpu(units["kf-" + name + ".service"])
    budgets["sandbox-factory"] = rendered_cpu(factory_service.render(receipt))
    dispatched = [name for group in lifecycle.START_GROUPS for name in group if name in budgets]
    assert sorted(dispatched) == sorted(budgets)
    # HTTPS checks its own bounded source rather than the full execution bundle.
    assert lifecycle.START_GROUPS[-1] == ("proxy",)
    for group in lifecycle.START_GROUPS:
        assert sum(budgets[name] for name in group if name in budgets) <= node_cpu


@pytest.mark.parametrize("running", [False, True])
def test_restore_allots_node_cpu_only_after_stopped_gate_model(monkeypatch, running):
    # Unit construction/preconditions are modeled. No host manager, data tree,
    # permissions, kernel limits or actual recovery are exercised here.
    restore = importlib.import_module("scripts.release.restore")
    trace = []
    node = ROOT / "deploy/release/kfonlinejudge.slice"
    quota = next(line for line in node.read_text().splitlines() if line.startswith("CPUQuota="))
    node_cpu = int(quota.partition("=")[2].removesuffix("%"))
    monkeypatch.setattr(restore.install, "selection", lambda: {"release_id": "sd-" + "b" * 12})

    def stopped():
        trace.append("stopped")
        if running:
            raise policy.DeliveryError("modeled managed writer")

    monkeypatch.setattr(restore, "stopped", stopped)
    monkeypatch.setattr(restore.install, "directory", lambda *args: trace.append("directory"))
    monkeypatch.setattr(
        restore.backup,
        "manager",
        lambda *args: {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
    )

    def launch(command, **options):
        assert trace[0] == "stopped"
        assert command[0] == "/usr/bin/systemd-run"
        properties = dict(
            arg.removeprefix("--property=").split("=", 1)
            for arg in command
            if arg.startswith("--property=")
        )
        assert properties["CPUQuota"] == str(node_cpu) + "%"
        assert properties["MemoryMax"] == "512M" and properties["MemorySwapMax"] == "0"
        assert properties["TasksMax"] == "32" and properties["RuntimeMaxSec"] == "1800"
        assert properties["NoNewPrivileges"] == "yes" and properties["Restart"] == "no"
        assert (
            properties["CapabilityBoundingSet"]
            == properties["AmbientCapabilities"]
            == ("CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER")
        )
        assert properties["ReadOnlyPaths"] == str(restore.backup.ROOT)
        assert options["timeout"] == 1820
        trace.append("dispatch")
        return SimpleNamespace(returncode=0, stdout=b"{}")

    monkeypatch.setattr(restore.subprocess, "run", launch)
    args = ("rr-" + "d" * 32, "bk-" + "e" * 32, "f" * 64)
    if running:
        with pytest.raises(policy.DeliveryError, match="modeled managed writer"):
            restore.launch(*args)
        assert trace == ["stopped"]
    else:
        assert restore.launch(*args) == {}
        assert trace[-1] == "dispatch"


@pytest.mark.parametrize("names", lifecycle.START_GROUPS)
def test_start_group_dispatches_independent_units_before_confirming_each(
    ordinary_lifecycle_model, names
):
    maintenance, _, trace, _ = ordinary_lifecycle_model
    maintenance.start_group(names)
    starts = [(i, name) for i, (kind, name) in enumerate(trace) if kind == "dispatch"]
    inspections = [(i, name) for i, (kind, name) in enumerate(trace) if kind == "inspect"]
    assert [name for _, name in starts] == list(names)
    assert {name for _, name in inspections} == set(names)
    assert min(i for i, _ in inspections) > max(i for i, _ in starts)
    assert all(event["confirmed"] for event in maintenance.record["events"])
    assert maintenance.record["root_cleanup_confirmed"] is ("sandbox-factory" not in names)


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


@pytest.mark.parametrize("names", lifecycle.START_GROUPS)
def test_group_profile_failure_cannot_confirm_a_started_unit(
    ordinary_lifecycle_model, monkeypatch, names
):
    maintenance, _, trace, _ = ordinary_lifecycle_model

    def refused(name, selected):
        raise policy.DeliveryError("ordinary effective profile refusal model")

    monkeypatch.setattr(lifecycle.node_health, "inspect_unit", refused)
    with pytest.raises(policy.DeliveryError, match="effective profile refusal"):
        maintenance.start_group(names)
    assert [name for kind, name in trace if kind == "dispatch"] == list(names)
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
        trace.append(("health", gate[-1]))
        observations.append(gate[-1])
        return {"service_preconditions_met": True, "ready_for_admission": gate[-1] == "open"}

    monkeypatch.setattr(lifecycle.operator_units, "run", observe)
    maintenance.start_locked(resume=True)
    assert gate == observations == ["draining", "open"]
    assert trace.index(("https", "proxy")) < trace.index(("health", "draining"))
    dispatches = [name for kind, name in trace if kind == "dispatch"]
    assert dispatches == [*lifecycle.DEPENDENCIES, *lifecycle.START_ORDER]
    for dependency in (*lifecycle.DEPENDENCIES, "sandbox-factory"):
        assert trace.index(("inspect", dependency)) < trace.index(("dispatch", "coordinator"))
    assert trace.index(("inspect", "api")) < trace.index(("dispatch", "proxy"))
    assert trace.index(("factory-status", "sandbox-factory")) < trace.index(
        ("dispatch", "coordinator")
    )


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


def test_api_initialization_precedes_first_python_worker_dispatch(
    ordinary_lifecycle_model, monkeypatch
):
    maintenance, _, trace, clock = ordinary_lifecycle_model
    probes = []

    def live(_url, *, timeout):
        probes.append(timeout)
        if len(probes) == 1:
            raise ConnectionRefusedError("ordinary startup model")
        trace.append(("api-ready", "api"))
        return {"status": "ok", "service": "platform"}

    monkeypatch.setattr(lifecycle.node_health, "http", live)
    monkeypatch.setattr(lifecycle.availability, "write", lambda *_: None)
    monkeypatch.setattr(
        lifecycle.operator_units,
        "run",
        lambda *_: {"service_preconditions_met": True, "ready_for_admission": True},
    )
    maintenance.start_locked(resume=True)
    assert clock[0] == 0.5 and len(probes) == 2
    assert trace.index(("api-ready", "api")) < trace.index(("dispatch", "outbox"))
    assert ("outbox", "problem-import") not in lifecycle.START_GROUPS


def test_api_wait_does_not_restart_its_deadline(ordinary_lifecycle_model, monkeypatch):
    maintenance, _, trace, clock = ordinary_lifecycle_model
    probes = []

    def unavailable(_url, *, timeout):
        probes.append(timeout)
        clock[0] += timeout
        raise TimeoutError("ordinary bounded startup model")

    monkeypatch.setattr(lifecycle.node_health, "http", unavailable)
    with pytest.raises(policy.DeliveryError, match="bounded API startup"):
        maintenance._wait_for_api(1.25)
    assert probes == [1.25] and clock[0] == 1.25
    assert not any(kind == "dispatch" for kind, _ in trace)


@pytest.mark.parametrize("failure", ["http", "shape", "binding"])
def test_api_startup_invalid_response_or_binding_is_not_retried(
    ordinary_lifecycle_model, monkeypatch, failure
):
    maintenance, _, _, _ = ordinary_lifecycle_model
    calls = []

    def invalid(url, *, timeout):
        calls.append(timeout)
        if failure == "http":
            raise lifecycle.urllib.error.HTTPError(url, 503, "ordinary", {}, None)
        if failure == "binding":
            monkeypatch.setattr(lifecycle.install, "selection", lambda **_: {})
        return {"status": "ok", "service": "other"}

    monkeypatch.setattr(lifecycle.node_health, "http", invalid)
    with pytest.raises(policy.DeliveryError):
        maintenance._wait_for_api(10)
    assert calls == [5]


@pytest.mark.parametrize("timeout", [True, 0, -1, 5.1, float("nan"), float("inf"), "5"])
def test_health_http_requires_finite_positive_bounded_timeout(timeout):
    with pytest.raises(policy.DeliveryError, match="bounded health HTTP timeout"):
        lifecycle.node_health.http("http://127.0.0.1:8000/api/v1/health/live", timeout=timeout)


@pytest.mark.parametrize("names", [(), ("api",), ("postgres", "postgres")])
def test_startup_rejects_unknown_or_duplicate_groups(ordinary_lifecycle_model, names):
    maintenance, _, trace, _ = ordinary_lifecycle_model
    with pytest.raises(policy.DeliveryError, match="unknown startup dependency group"):
        maintenance.start_group(names)
    assert trace == []


@pytest.mark.parametrize("rotation", [False, True])
def test_proxy_install_cli_keeps_legacy_inputs_and_requires_explicit_rotation(
    monkeypatch, capsys, rotation
):
    calls = []

    def route(*args, **kwargs):
        calls.append((args, kwargs))
        return {"routed": True}

    monkeypatch.setattr(proxy_install, "install_proxy", route)
    arguments = [
        "--source",
        "/ordinary/proxy",
        "--receipt-sha256",
        "a" * 64,
        "--certificate",
        "/ordinary/certificate",
        "--private-key",
        "/ordinary/key",
    ]
    expected = {}
    if rotation:
        arguments += ["--previous-certificate-sha256", "b" * 64, "--previous-key-sha256", "c" * 64]
        expected = {"previous_certificate_sha256": "b" * 64, "previous_key_sha256": "c" * 64}
    proxy_install.main(arguments)
    assert calls == [
        (
            (
                Path("/ordinary/proxy"),
                "a" * 64,
                Path("/ordinary/certificate"),
                Path("/ordinary/key"),
            ),
            expected,
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"routed": True}


def test_proxy_install_cli_rejects_an_incomplete_rotation_before_dispatch(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(proxy_install, "install_proxy", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit) as failure:
        proxy_install.main(
            [
                "--source",
                "/ordinary/proxy",
                "--receipt-sha256",
                "a" * 64,
                "--certificate",
                "/ordinary/certificate",
                "--private-key",
                "/ordinary/key",
                "--previous-certificate-sha256",
                "b" * 64,
            ]
        )
    assert failure.value.code == 1
    assert calls == []
    assert "paired previous TLS fingerprints required" in capsys.readouterr().err


def test_proxy_previous_snapshot_retains_the_legacy_unit_and_binding_format(tmp_path, monkeypatch):
    # This is a trusted unit-format fixture; Root ownership is verified only in VM.
    service = proxy_install.proxy_service
    selected = {
        "schema_version": 1,
        "release_id": "sd-" + "1" * 12,
        "configuration_id": "cfg-" + "2" * 24,
        "manifest_sha256": "3" * 64,
        "receipt_sha256": "4" * 64,
    }
    monkeypatch.setattr(service, "UNITS", tmp_path / "units")
    monkeypatch.setattr(proxy_install.preflight, "protected", lambda *args, **kwargs: None)
    directory = service.UNITS / service.batch(selected).name
    directory.mkdir(parents=True)
    binding = directory / "binding.json"
    binding.write_bytes(policy.canonical(selected))
    binding.chmod(0o600)
    unit = directory / service.UNIT
    unit.write_bytes(service.render(selected))
    unit.chmod(0o644)
    assert proxy_install.known_snapshot(directory) == selected
    assert "--previous-certificate" not in unit.read_text()


@pytest.mark.parametrize("support", ["legacy", "complete", "binary-only", "transport-only"])
def test_native_restore_capability_requires_a_complete_pair_but_keeps_legacy(
    ordinary_bundle, support
):
    bundle, _ = ordinary_bundle
    data = json.loads((bundle / "manifest.json").read_bytes())
    names = {
        "legacy": [],
        "complete": ["bin/kf-restore-io", "delivery/scripts/release/restore_native.py"],
        "binary-only": ["bin/kf-restore-io"],
        "transport-only": ["delivery/scripts/release/restore_native.py"],
    }[support]
    parents = set(data["directories"])
    for name in names:
        data["files"].append(
            {
                "path": name,
                "bytes": 1,
                "sha256": "b" * 64,
                "mode": "0555" if name.startswith("bin/") else "0444",
            }
        )
        data["payload_bytes"] += 1
        parents.update(str(p) for p in Path(name).parents if str(p) != ".")
    data["directories"] = sorted(parents)
    if support in {"legacy", "complete"}:
        assert manifest.validate(data) is data
    else:
        with pytest.raises(policy.DeliveryError, match="incomplete application bundle"):
            manifest.validate(data)
