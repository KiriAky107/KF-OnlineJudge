"""Trusted adapter checks; real cold PG/role/permission tests remain VM-only."""

import hashlib

from kf_oj.contracts.judging import ArtifactReference
from kf_oj.infra.artifacts.backup import verify_existing


def test_backup_verifies_binary_and_empty_objects_without_creating_storage_entries(tmp_path):
    tmp_path.chmod(0o700)
    values = [b"", b"\0\xff\n" * 50000]
    expected = {}
    for value in values:
        digest = hashlib.sha256(value).hexdigest()
        path = tmp_path / digest
        path.write_bytes(value)
        path.chmod(0o600)
        expected[digest] = value
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    for digest, value in expected.items():
        verify_existing(tmp_path, ArtifactReference(digest=digest, bytes=len(value)))
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert after == before
    assert not (tmp_path / ".lock").exists()
