"""Ordinary trusted byte fixtures; no privileged backup, Guest, or RTO proof."""

import hashlib
import os
import time

import pytest
from scripts.release import backup_objects as objects
from scripts.release.policy import DeliveryError

DATA = bytes(range(256)) * 1024


def capture(path, target, seen, hints):
    return objects.store(
        path,
        path.stat(),
        target,
        seen,
        time.monotonic() + 20,
        remaining_bytes=8 * len(DATA),
        candidates=hints,
    )


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "sources"
    directory.mkdir()
    path = directory / "same-name.bin"
    path.write_bytes(DATA)
    target = tmp_path / "objects"
    target.mkdir(mode=0o700)
    return path, target, {}, {}


def test_equal_source_reads_every_byte_without_rehash_or_another_object(source, monkeypatch):
    path, target, seen, hints = source
    digest, added = capture(path, target, seen, hints)
    assert added == len(DATA)
    calls = []
    actual = objects.same_object_bytes

    def checked(stream, before, candidate, deadline):
        result = actual(stream, before, candidate, deadline)
        calls.append((stream.tell(), candidate, result))
        return result

    monkeypatch.setattr(objects, "same_object_bytes", checked)
    monkeypatch.setattr(
        objects, "read_stream", lambda *a, **k: pytest.fail("equal source rehashed")
    )
    assert capture(path, target, seen, hints) == (digest, 0)
    assert calls == [(len(DATA), target / digest, True)]
    assert list(target.iterdir()) == [target / digest]
    assert (target / digest).read_bytes() == DATA


def test_distinct_readonly_version_file_reuses_content_and_keeps_its_metadata(source):
    path, target, seen, hints = source
    digest, _ = capture(path, target, seen, hints)
    other_version = path.parent / "another-version"
    other_version.mkdir()
    other = other_version / path.name
    other.write_bytes(DATA)
    other.chmod(0o555)
    before = other.stat()
    assert before.st_ino != path.stat().st_ino
    assert capture(other, target, seen, hints) == (digest, 0)
    assert objects.identity(other.stat()) == objects.identity(before)
    stored = target / digest
    assert stored.stat().st_mode & 0o777 == 0o600
    assert stored.stat().st_nlink == 1 and stored.read_bytes() == DATA


@pytest.mark.parametrize("offset", [0, len(DATA) - 1])
def test_same_name_and_size_never_establish_content_identity(source, offset):
    path, target, seen, hints = source
    old, _ = capture(path, target, seen, hints)
    changed = bytearray(DATA)
    changed[offset] ^= 255
    path.write_bytes(changed)
    new, added = capture(path, target, seen, hints)
    assert new == hashlib.sha256(changed).hexdigest() != old
    assert added == len(DATA) and len(list(target.iterdir())) == 2
    assert (target / old).read_bytes() == DATA
    assert (target / new).read_bytes() == changed
    path.write_bytes(DATA)
    assert capture(path, target, seen, hints) == (old, 0)


def test_corrupted_existing_object_cannot_supply_a_digest(source):
    path, target, seen, hints = source
    digest, _ = capture(path, target, seen, hints)
    (target / digest).write_bytes(DATA[:-1] + b"!")
    with pytest.raises(DeliveryError, match="differs from its captured digest"):
        capture(path, target, seen, hints)
    assert not (target / ".pending").exists()


@pytest.mark.parametrize("kind", ["mode", "hardlink", "symlink"])
def test_comparison_object_still_requires_single_private_regular_file(source, kind):
    path, target, seen, hints = source
    digest, _ = capture(path, target, seen, hints)
    stored = target / digest
    if kind == "mode":
        stored.chmod(0o644)
    elif kind == "hardlink":
        os.link(stored, target.parent / "other-owned-link")
    else:
        stored.unlink()
        stored.symlink_to(path)
    with pytest.raises(DeliveryError, match="protection differs"):
        capture(path, target, seen, hints)


def test_changed_source_metadata_is_rejected_even_when_bytes_match(source):
    path, target, seen, hints = source
    digest, _ = capture(path, target, seen, hints)
    before = path.stat()
    with path.open("rb") as stream:

        class Changed:
            changed = False

            def fileno(self):
                return stream.fileno()

            def read(self, size):
                value = stream.read(size)
                if not self.changed:
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
                    self.changed = True
                return value

        with pytest.raises(DeliveryError, match="changed during read"):
            objects.same_object_bytes(Changed(), before, target / digest, time.monotonic() + 20)


def test_expired_comparison_keeps_private_target_unmodified(source):
    path, target, seen, hints = source
    digest, _ = capture(path, target, seen, hints)
    with pytest.raises(DeliveryError, match="deadline"):
        objects.store(
            path,
            path.stat(),
            target,
            seen,
            time.monotonic() - 1,
            remaining_bytes=8 * len(DATA),
            candidates=hints,
        )
    assert (target / digest).read_bytes() == DATA
    assert not (target / ".pending").exists()


def test_hint_cannot_reference_uncaptured_or_non_digest_path(source):
    path, target, seen, hints = source
    hints[(path.name, len(DATA))] = "../outside"
    with pytest.raises(DeliveryError):
        capture(path, target, seen, hints)
    hints[(path.name, len(DATA))] = hashlib.sha256(b"not captured in this invocation").hexdigest()
    with pytest.raises(DeliveryError, match="no current captured object"):
        capture(path, target, seen, hints)
    assert not list(target.iterdir())


def test_full_hint_table_only_falls_back_to_complete_hashing(source, monkeypatch):
    path, target, seen, hints = source
    monkeypatch.setattr(objects, "MAX_COMPARISON_HINTS", 1)
    capture(path, target, seen, hints)
    another = path.with_name("different-name.bin")
    another.write_bytes(DATA + b"next")
    digest, added = capture(another, target, seen, hints)
    assert len(hints) == 1
    assert added == len(DATA) + 4
    assert digest == hashlib.sha256(DATA + b"next").hexdigest()
    assert (target / digest).read_bytes() == DATA + b"next"


def test_small_files_and_callers_without_hints_keep_original_hash_path(source):
    path, target, seen, hints = source
    path.write_bytes(b"small trusted bytes")
    digest, added = capture(path, target, seen, hints)
    assert hints == {} and added == path.stat().st_size
    assert objects.store(
        path, path.stat(), target, seen, time.monotonic() + 20, remaining_bytes=8 * len(DATA)
    ) == (digest, 0)
