"""Finite adverse archive/private-storage acceptance: independent capped VM only."""

import hashlib
import io
import json
import os
import stat
import struct
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from test_admin_import_vm import independent_import_vm as independent_import_vm
from test_admin_import_vm import package

from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.infra.artifacts.archive_reader import ArchiveError, ArchiveReader
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore

ROOT = Path(__file__).resolve().parents[2]


def policy():
    return AdmissionRegistry.model_validate_json(
        (ROOT / "tests/fixtures/contracts-v2/judge-registry.valid.json").read_bytes()
    )


def proof(name, value):
    print(
        "KF_FINAL_ACCEPTANCE " + json.dumps({"name": name, "result": value}, default=str),
        flush=True,
    )


def test_adverse_zip_and_private_storage_boundaries(independent_import_vm, tmp_path):
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"unchanged synthetic sentinel")
    original = hashlib.sha256(outside.read_bytes()).hexdigest()
    good, _ = package(policy())
    cases = {}
    for name in ("../outside-sentinel", "/outside-sentinel", "a/b", r"a\b", "a:b"):
        cases["path-" + repr(name)] = package(
            policy(), payload_mutation=lambda p, n=name: p.update({n: b"bad"})
        )[0]
    for kind in (
        stat.S_IFLNK,
        stat.S_IFCHR,
        stat.S_IFBLK,
        stat.S_IFIFO,
        stat.S_IFSOCK,
        stat.S_IFDIR,
    ):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            entry = zipfile.ZipInfo("unsafe")
            entry.create_system = 3
            entry.external_attr = (kind | 0o600) << 16
            archive.writestr(entry, b"synthetic")
        cases[f"type-{kind}"] = out.getvalue()
    for duplicate in ("statement.md", "STATEMENT.MD"):
        out = io.BytesIO(good)
        with zipfile.ZipFile(out, "a") as archive:
            archive.writestr(duplicate, b"different")
        cases["duplicate-" + duplicate] = out.getvalue()
    for signature in (
        b"PK\x03\x04",
        b"\x1f\x8b",
        b"BZh",
        b"\xfd7zXZ\x00",
        b"Rar!\x1a\x07",
        b"7z\xbc\xaf\x27\x1c",
        b"X" * 257 + b"ustar",
    ):
        cases["nested-" + signature[:6].hex()] = package(
            policy(),
            payload_mutation=lambda p, s=signature: p.update({"hidden.in": s + b"synthetic"}),
        )[0]
    central = good.index(b"PK\x01\x02")
    for label, local_offset, central_offset, value, fmt in (
        ("encrypted", 6, 8, 1, "<H"),
        ("unsupported", 8, 10, 99, "<H"),
        ("declared-size", 22, 24, 67108865, "<I"),
        ("crc", 14, 16, 1, "<I"),
    ):
        blob = bytearray(good)
        struct.pack_into(fmt, blob, local_offset, value)
        struct.pack_into(fmt, blob, central + central_offset, value)
        cases[label] = bytes(blob)
    for label, extra in (("zip64", b"\x01\x00\x00\x00"), ("unknown-extra", b"\x99\x99\x00\x00")):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            entry = zipfile.ZipInfo("data")
            entry.extra = extra
            archive.writestr(entry, b"synthetic")
        cases[label] = out.getvalue()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for i in range(535):
            archive.writestr(f"f{i}", b"")
    cases["file-count"] = out.getvalue()
    # Actual bounded inflation exceeds the forged declared size. The 65 MiB stream
    # is produced in chunks under the VM cap; no 65 MiB object is allocated.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open("inflate", "w") as member:
            for _ in range(1040):
                member.write(b"A" * 65536)
    blob = bytearray(out.getvalue())
    center = blob.index(b"PK\x01\x02")
    struct.pack_into("<I", blob, 22, 67108864)
    struct.pack_into("<I", blob, center + 24, 67108864)
    cases["actual-inflate-over-64MiB"] = bytes(blob)
    refused = []
    for index, (label, blob) in enumerate(cases.items()):
        file = tmp_path / f"archive-{index}"
        file.write_bytes(blob)
        file.chmod(0o600)
        fd = os.open(file, os.O_RDONLY)
        try:
            with pytest.raises(ArchiveError) as error:
                reader = ArchiveReader(fd, len(blob), deadline=time.monotonic() + 60)
                for member in reader.members:
                    for _ in reader.chunks(member.name, 67108864):
                        pass
            assert str(error.value) in ("invalid_archive", "archive_deadline")
            refused.append(label)
        finally:
            os.close(fd)
            file.unlink()
    file = tmp_path / "mutable"
    file.write_bytes(good)
    file.chmod(0o600)
    fd = os.open(file, os.O_RDONLY)
    try:
        reader = ArchiveReader(fd, len(good), deadline=time.monotonic() + 60)
        with file.open("r+b") as writer:
            writer.write(b"X")
        with pytest.raises(ArchiveError, match="archive_changed"):
            reader.verify_unchanged()
    finally:
        os.close(fd)
    store = ArtifactStore(tmp_path / "objects", max_bytes=4, max_files=2)
    try:
        ref = store.put(b"1234")
        with pytest.raises(ArtifactError, match="capacity"):
            store.put(b"5")
        assert store.read(ref) == b"1234"
        store.remove(ref.digest)
        recovered = store.put(b"5")
        assert store.read(recovered) == b"5"
    finally:
        store.close()
    link_root = tmp_path / "linked-store"
    link_root.symlink_to(tmp_path / "objects", target_is_directory=True)
    with pytest.raises((ArtifactError, OSError)):
        ArtifactStore(link_root)
    staging = ImportStaging(tmp_path / "staging")
    operation_id = uuid4()
    (tmp_path / "staging" / str(operation_id)).symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises((ArtifactError, OSError)):
            staging.operation(operation_id)
    finally:
        staging.close()
    assert hashlib.sha256(outside.read_bytes()).hexdigest() == original
    proof(
        "archive_and_storage",
        {
            "refused": refused,
            "outside_unchanged": True,
            "content_never_executed": True,
            "changed_descriptor_rejected": True,
            "quota_recovered": True,
            "symlink_roots_rejected": True,
        },
    )
