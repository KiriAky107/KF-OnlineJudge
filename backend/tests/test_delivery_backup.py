"""Trusted byte fixtures only; no privileged backup, recovery or RTO acceptance."""

import hashlib
import importlib
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
backup_manifest = importlib.import_module("scripts.release.backup_manifest")
backup_objects = importlib.import_module("scripts.release.backup_objects")
policy = importlib.import_module("scripts.release.policy")


def empty_drain_projection():
    return {
        "execution": {
            "database_clock_unix_ms": 1791830000000,
            **{
                name: {"count_capped": 0, "truncated": False}
                for name in ("queued", "in_flight", "cleanup_unconfirmed")
            },
        },
        "outbox": {
            "schema_version": 1,
            "pending_capped": 0,
            "locked_capped": 0,
            "truncated": False,
        },
        "imports": {
            "schema_version": 1,
            "worker_ready": True,
            "reserved_bytes": 0,
            "uploaded_capped": 0,
            "validating_capped": 0,
            "truncated": False,
        },
        "broker": {
            "schema_version": 1,
            "metadata_verified": True,
            "messages_ready": 0,
            "messages_unacknowledged": 0,
            "message_bytes": 0,
            "consumers": 1,
        },
        "factory": {
            "schema_version": 1,
            "configuration_sha256": "b" * 64,
            "supervisor_authorized": True,
            "execution_gate": "required_per_execution",
            "isolation_verified": False,
            "max_concurrent_runs": 1,
            "active_helpers": 0,
            "cleanup_complete": True,
            "remaining_requests": 4096,
            "remaining_lifetime_seconds": 3600,
        },
    }


@pytest.fixture
def snapshot():
    data = b"trusted repeated bytes\n"
    digest = hashlib.sha256(data).hexdigest()
    files = [
        {
            "path": path,
            "uid": uid,
            "gid": uid,
            "mode": mode,
            "bytes": len(data),
            "sha256": digest,
        }
        for path, uid, mode in [
            ("postgres/PG_VERSION", 1203, 0o600),
            ("postgres/global/pg_control", 1203, 0o600),
            ("releases/first", 0, 0o444),
            ("releases/second", 0, 0o555),
        ]
    ]
    boot = "12345678-1234-1234-1234-123456789abc"
    selected = {
        "schema_version": 1,
        "release_id": "sd-" + "c" * 12,
        "manifest_sha256": "b" * 64,
        "configuration_id": "cfg-" + "d" * 24,
        "receipt_sha256": "e" * 64,
    }
    return {
        "schema_version": 3,
        "format": backup_manifest.FORMAT,
        "backup_id": "bk-" + "2" * 32,
        "source_sha": "c" * 40,
        "manifest_sha256": selected["manifest_sha256"],
        "configuration_id": selected["configuration_id"],
        "created_unix_seconds": 1793070000,
        "expires_unix_seconds": 1793070000 + 7 * 86400,
        "selected": selected,
        "consistent_cut": {
            "schema_version": 1,
            "source_sha": "c" * 40,
            "configuration_id": selected["configuration_id"],
            "action": "stop",
            "phase": "complete",
            "success": True,
            "events": [],
            "root_cleanup_confirmed": True,
            "boot_id": boot,
            "drain_projection": empty_drain_projection(),
        },
        "artifact_references": {
            "schema_version": 1,
            "kind": "kf.private.artifact-backup-inventory",
            "read_only": True,
            "quiescence_verified": False,
            "boot_id": boot,
            "storage_identity": {"device": 1, "inode": 2, "uid": 1200},
            "objects": [],
        },
        "broker_cookie_sha256": "b" * 64,
        "database_bootstrap_sha256": "e" * 64,
        "images": {
            key: {
                "toolchain_id": key,
                "image_kind": key,
                "runtime_version": "1",
                "image_revision": 1,
                "image_digest": "f" * 64,
            }
            for key in ("gnu", "python313", "java21")
        },
        "directories": [
            {"path": path, "uid": 0, "gid": 0, "mode": 0o700}
            for path in sorted(backup_manifest.DOMAINS | {"postgres/global"})
        ],
        "files": files,
        "payload_bytes": len(data) * len(files),
        "restored": False,
        "execution_verified": False,
    }


def object_format(value):
    return {
        **value,
        "schema_version": 4,
        "format": backup_manifest.OBJECT_FORMAT,
        "logical_bytes": value["payload_bytes"],
        "payload_bytes": value["files"][0]["bytes"],
    }


def test_v3_reader_and_v4_logical_permissions_survive_content_deduplication(snapshot, tmp_path):
    old = backup_manifest.validate(snapshot)
    new = backup_manifest.validate(object_format(snapshot))
    assert len(backup_manifest.physical_files(old)) == 4
    assert len(backup_manifest.physical_files(new)) == 1
    assert backup_manifest.logical_bytes(old) == backup_manifest.logical_bytes(new)
    assert new["payload_bytes"] * 4 == new["logical_bytes"]
    assert new["files"][2]["mode"] != new["files"][3]["mode"]
    for item in new["files"]:
        assert backup_manifest.member_path(tmp_path, new, item) == (
            tmp_path / "objects" / item["sha256"]
        )
        assert backup_manifest.named_member(tmp_path, old, item["path"]) == (
            tmp_path / "payload" / item["path"]
        )


@pytest.mark.parametrize(
    "field,change",
    [
        ("payload_bytes", 1),
        ("logical_bytes", 1),
        ("schema_version", True),
        ("format", backup_manifest.FORMAT),
    ],
)
def test_v4_cannot_confuse_stored_bytes_with_restored_bytes(snapshot, field, change):
    value = object_format(snapshot)
    value[field] = change
    with pytest.raises(policy.DeliveryError):
        backup_manifest.validate(value)


def test_v4_preserves_logical_versions_above_v3_limit_without_raising_physical_limit(snapshot):
    value = object_format(snapshot)
    size = backup_manifest.MAX_FILE_BYTES
    value["files"] = [
        {**value["files"][0], "path": "releases/version-" + str(index), "bytes": size}
        for index in range(32)
    ] + [{**item, "bytes": size} for item in snapshot["files"][:2]]
    value["logical_bytes"] = len(value["files"]) * size
    value["payload_bytes"] = size
    backup_manifest.validate(value)
    assert value["logical_bytes"] > backup_manifest.MAX_BYTES
    old = {
        **value,
        "schema_version": 3,
        "format": backup_manifest.FORMAT,
        "payload_bytes": value["logical_bytes"],
    }
    old.pop("logical_bytes")
    with pytest.raises(policy.DeliveryError):
        backup_manifest.validate(old)
    # Distinct object digests still consume the unchanged physical budget.
    for index, item in enumerate(value["files"]):
        item["sha256"] = hashlib.sha256(str(index).encode()).hexdigest()
    value["payload_bytes"] = value["logical_bytes"]
    with pytest.raises(policy.DeliveryError):
        backup_manifest.validate(value)


def test_store_reads_each_source_but_writes_one_single_link_object(tmp_path, monkeypatch):
    source, repeated, objects = tmp_path / "source", tmp_path / "repeated", tmp_path / "objects"
    objects.mkdir(mode=0o700)
    data = b"trusted source\n" * 2000
    source.write_bytes(data)
    repeated.write_bytes(data)
    repeated.chmod(0o444)
    original = backup_objects.read_stream
    observed = []

    def read(incoming, before, deadline, outgoing=None):
        observed.append((before.st_ino, outgoing is not None))
        return original(incoming, before, deadline, outgoing)

    monkeypatch.setattr(backup_objects, "read_stream", read)
    seen = {}
    first, added = backup_objects.store(
        source, source.stat(), objects, seen, time.monotonic() + 10, remaining_bytes=len(data)
    )
    second, repeated_bytes = backup_objects.store(
        repeated, repeated.stat(), objects, seen, time.monotonic() + 10, remaining_bytes=0
    )
    assert first == second == hashlib.sha256(data).hexdigest()
    assert (added, repeated_bytes) == (len(data), 0)
    assert observed == [
        (source.stat().st_ino, False),
        (source.stat().st_ino, True),
        (repeated.stat().st_ino, False),
    ]
    assert list(objects.iterdir()) == [objects / first]
    assert (objects / first).read_bytes() == data
    assert (objects / first).stat().st_nlink == 1
    assert (objects / first).stat().st_mode & 0o777 == 0o600


def test_failed_second_source_read_retains_incomplete_private_copy(tmp_path, monkeypatch):
    source, objects = tmp_path / "source", tmp_path / "objects"
    source.write_bytes(b"trusted fixture")
    objects.mkdir(mode=0o700)
    original = backup_objects.read_stream

    def read(incoming, before, deadline, outgoing=None):
        if outgoing is not None:
            raise OSError("fixture write failure")
        return original(incoming, before, deadline)

    monkeypatch.setattr(backup_objects, "read_stream", read)
    seen = {}
    with pytest.raises(OSError):
        backup_objects.store(
            source, source.stat(), objects, seen, time.monotonic() + 10, remaining_bytes=100
        )
    assert seen == {}
    assert list(objects.iterdir()) == [objects / ".pending"]
    assert not (tmp_path / "manifest.json").exists()


def test_restore_expands_repeated_content_into_independent_files_with_own_modes(tmp_path):
    restore = importlib.import_module("scripts.release.restore")
    content = tmp_path / "object"
    data = b"trusted restoration fixture\n"
    content.write_bytes(data)
    row = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    first, second = tmp_path / "first", tmp_path / "second"
    for path, mode in ((first, 0o600), (second, 0o444)):
        witness = restore.write_file(
            content, path, row, os.getuid(), os.getgid(), mode, time.monotonic() + 10
        )
        restore.unchanged_file(path, witness, os.getuid(), os.getgid(), mode)
        assert path.read_bytes() == data
        assert path.stat().st_mode & 0o777 == mode
        assert path.stat().st_nlink == 1
    assert len({path.stat().st_ino for path in (content, first, second)}) == 3


def test_delayed_filesystem_write_error_is_not_success(tmp_path, monkeypatch):
    class SyncFailure:
        def __call__(self, descriptor):
            assert os.fstat(descriptor).st_ino == tmp_path.stat().st_ino
            return -1

    class Library:
        syncfs = SyncFailure()

    monkeypatch.setattr(backup_objects.ctypes, "CDLL", lambda *_, **__: Library())
    with pytest.raises(policy.DeliveryError, match="filesystem synchronization failed"):
        backup_objects.sync_filesystem(tmp_path, time.monotonic() + 10)


@pytest.mark.parametrize("missing", ["releases/older", "releases/older/nested"])
def test_every_directory_ancestor_is_required_even_when_file_parent_exists(snapshot, missing):
    value = object_format(snapshot)
    value["directories"] = value["directories"] + [
        {"path": path, "uid": 0, "gid": 0, "mode": 0o700}
        for path in ("releases/older", "releases/older/nested")
        if path != missing
    ]
    value["files"] = [
        {**row, "path": "releases/older/nested/file"} if row["path"] == "releases/first" else row
        for row in value["files"]
    ]
    with pytest.raises(policy.DeliveryError, match="private backup parent missing"):
        backup_manifest.validate(value)


def test_v4_complete_version_inventory_keeps_an_explicit_logical_entry_bound(snapshot):
    value = object_format(snapshot)
    count = backup_manifest.MAX_LOGICAL_ENTRIES - len(value["directories"])
    template = {**value["files"][0], "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    value["files"] = [{**template, "path": row["path"]} for row in value["files"][:2]] + [
        {**template, "path": "releases/item-" + str(i)} for i in range(count - 2)
    ]
    value.update(logical_bytes=count, payload_bytes=1)
    backup_manifest.validate(value)
    assert len(value["files"]) + len(value["directories"]) == 98304
    assert backup_manifest.MAX_BYTES == 7 * 1024**3
    assert backup_manifest.MAX_LOGICAL_BYTES == 12 * 1024**3
    old = {**value, "schema_version": 3, "format": backup_manifest.FORMAT, "payload_bytes": count}
    old.pop("logical_bytes")
    with pytest.raises(policy.DeliveryError, match="inventory count exceeds bound"):
        backup_manifest.validate(old)
    value["files"].append({**template, "path": "releases/one-too-many"})
    value["logical_bytes"] += 1
    with pytest.raises(policy.DeliveryError, match="inventory count exceeds bound"):
        backup_manifest.validate(value)


def test_private_metadata_rejects_a_file_above_the_explicit_byte_limit(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_bytes(b" " * (backup_manifest.MAX_METADATA_BYTES + 1))
    with pytest.raises(policy.DeliveryError, match="metadata byte limit"):
        backup_manifest.read_metadata(path)
