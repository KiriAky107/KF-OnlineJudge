"""Trusted ordinary byte fixtures; real Root/cgroup/syncfs/RTO remains VM-only."""

import errno
import hashlib
import multiprocessing
import os
import stat
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
from scripts.release import restore, restore_io
from scripts.release.policy import DeliveryError


def fixture_rows(tmp_path, contents):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    rows = []
    for index, raw in enumerate(contents):
        digest = hashlib.sha256(raw).hexdigest()
        path = source / digest
        if not path.exists():
            path.write_bytes(raw)
        rows.append({"path": str(index), "bytes": len(raw), "sha256": digest})
    return (
        rows,
        lambda row: source / row["sha256"],
        lambda row: (
            target / row["path"],
            os.getuid(),
            os.getgid(),
            0o600 if int(row["path"]) % 2 else 0o400,
        ),
    )


def filesystem_model(monkeypatch, target, events, callback=None):
    """Model syncfs using only tiny owned fixture fsyncs; never host syncfs."""

    class Sync:
        def __call__(self, descriptor):
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
            if callback is not None:
                callback()
            files = [p for p in target.iterdir() if p.is_file()]
            for path in files:
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            events.append(("sync", descriptor))
            return 0

    monkeypatch.setattr(
        restore_io.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(syncfs=Sync())
    )
    return restore_io.Filesystems([target], time.monotonic() + 120)


def assert_closed(descriptors):
    for descriptor in descriptors:
        with pytest.raises(OSError) as failure:
            os.fstat(descriptor)
        assert failure.value.errno == errno.EBADF


class ReadAfterSync:
    """Picklable observation of actual tiny fixture reads in the child processes."""

    def __init__(self, synced, reads, descriptors):
        self.synced, self.reads, self.descriptors = synced, reads, descriptors

    def __call__(self, path, item, uid, gid, mode, deadline):
        assert self.synced.read_bytes() == b"sync completed"
        assert all(os.fstat(fd).st_ino > 0 for fd in self.descriptors)
        (self.reads / item["path"]).touch()
        return restore.readback(path, item, uid, gid, mode, deadline)


def test_bounded_batches_group_identical_objects_without_losing_paths():
    rows = [
        {"path": str(index), "sha256": str(index % 2), "bytes": 1}
        for index in range(restore_io.BATCH_FILES + 4)
    ]
    rows.append({"path": "large", "sha256": "2", "bytes": restore_io.BATCH_BYTES + 1})
    groups = list(restore_io.batches(rows))
    flattened = [row for group in groups for row in group]
    assert {row["path"] for row in flattened} == {row["path"] for row in rows}
    assert len(flattened) == len(rows)
    assert [row["sha256"] for row in flattened] == sorted(row["sha256"] for row in rows)
    assert all(len(group) <= restore_io.BATCH_FILES for group in groups)
    assert all(
        sum(row["bytes"] for row in group) <= restore_io.BATCH_BYTES or len(group) == 1
        for group in groups
    )


def test_repeated_objects_expand_to_independent_durable_verified_destinations(
    tmp_path, monkeypatch
):
    rows, source, destination = fixture_rows(tmp_path, [b"first", b"other", b"first", b"first"])
    events = []
    target = destination(rows[0])[0].parent
    synced, reads = tmp_path / "synced", tmp_path / "reads"
    reads.mkdir()
    with closing(filesystem_model(monkeypatch, target, events)) as filesystems:
        descriptors = [descriptor for _, descriptor, _ in filesystems.handles.values()]
        assert list(target.iterdir()) == []

        original_sync = filesystems.sync

        def sync(deadline):
            original_sync(deadline)
            synced.write_bytes(b"sync completed")

        monkeypatch.setattr(filesystems, "sync", sync)

        verified = restore_io.materialize(
            rows,
            source,
            destination,
            ReadAfterSync(synced, reads, descriptors),
            filesystems,
            time.monotonic() + 120,
        )
    assert_closed(descriptors)
    assert set(verified) == {row["path"] for row in rows}
    assert len(list(reads.iterdir())) == len(rows)
    inodes = set()
    for row in rows:
        path, uid, gid, mode = destination(row)
        assert path.read_bytes() == source(row).read_bytes()
        assert stat.S_IMODE(path.stat().st_mode) == mode
        assert path.stat().st_nlink == 1
        inodes.add(path.stat().st_ino)
        restore.unchanged_file(path, verified[row["path"]], uid, gid, mode)
    assert len(inodes) == len(rows)


def test_existing_wrong_bytes_are_refused_without_overwrite(tmp_path, monkeypatch):
    prior_children = {child.pid for child in multiprocessing.active_children()}
    rows, source, destination = fixture_rows(tmp_path, [b"new"])
    path, _, _, mode = destination(rows[0])
    path.write_bytes(b"bad")
    path.chmod(mode)
    before = restore.file_witness(path.stat())
    with closing(filesystem_model(monkeypatch, path.parent, [])) as filesystems:
        with pytest.raises(DeliveryError, match="byte verification"):
            restore_io.materialize(
                rows, source, destination, restore.readback, filesystems, time.monotonic() + 120
            )
    assert path.read_bytes() == b"bad" and restore.file_witness(path.stat()) == before
    assert {child.pid for child in multiprocessing.active_children()} == prior_children


def test_failed_sync_keeps_private_files_and_cannot_return_verified_witnesses(
    tmp_path, monkeypatch
):
    rows, source, destination = fixture_rows(tmp_path, [b"a", b"b", b"c"])
    reads = []

    def failed_sync():
        raise DeliveryError("synthetic synchronization failure")

    with closing(
        filesystem_model(monkeypatch, destination(rows[0])[0].parent, [], failed_sync)
    ) as filesystems:
        descriptors = [fd for _, fd, _ in filesystems.handles.values()]
        with pytest.raises(DeliveryError, match="synthetic synchronization failure"):
            restore_io.materialize(
                rows,
                source,
                destination,
                lambda *args: reads.append(args),
                filesystems,
                time.monotonic() + 120,
            )
    assert_closed(descriptors)
    assert reads == []
    assert all(destination(row)[0].is_file() for row in rows)


def test_same_bytes_inode_replacement_across_sync_cannot_adopt_new_file(tmp_path, monkeypatch):
    rows, source, destination = fixture_rows(tmp_path, [b"same"])

    def replace_inode():
        path, _, _, mode = destination(rows[0])
        replacement = path.parent / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(mode)
        replacement.replace(path)

    with closing(
        filesystem_model(monkeypatch, destination(rows[0])[0].parent, [], replace_inode)
    ) as filesystems:
        with pytest.raises(DeliveryError, match="changed across synchronization"):
            restore_io.materialize(
                rows, source, destination, restore.readback, filesystems, time.monotonic() + 120
            )


def test_expired_batch_deadline_copies_no_files(tmp_path, monkeypatch):
    rows, source, destination = fixture_rows(tmp_path, [b"data"])
    with closing(filesystem_model(monkeypatch, destination(rows[0])[0].parent, [])) as filesystems:
        with pytest.raises(DeliveryError, match="deadline exceeded"):
            restore_io.materialize(
                rows, source, destination, restore.readback, filesystems, time.monotonic() - 1
            )
    assert not destination(rows[0])[0].exists()


def test_syncfs_failure_is_propagated_in_the_ordinary_syscall_model(tmp_path, monkeypatch):
    class FailedSync:
        def __call__(self, _descriptor):
            return -1

    monkeypatch.setattr(
        restore_io.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(syncfs=FailedSync())
    )
    with closing(restore_io.Filesystems([tmp_path], time.monotonic() + 120)) as filesystems:
        descriptors = [fd for _, fd, _ in filesystems.handles.values()]
        with pytest.raises(DeliveryError, match="filesystem synchronization failed"):
            filesystems.sync(time.monotonic() + 120)
    assert_closed(descriptors)


def test_cursor_is_open_before_first_write_and_held_across_batches(tmp_path, monkeypatch):
    rows, source, destination = fixture_rows(tmp_path, [b"a", b"b", b"c", b"d", b"e"])
    target = destination(rows[0])[0].parent
    monkeypatch.setattr(restore_io, "BATCH_FILES", 2)
    events = []
    with closing(filesystem_model(monkeypatch, target, events)) as filesystems:
        descriptors = [fd for _, fd, _ in filesystems.handles.values()]
        before = {fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors}
        real_write = restore_io.pending_file

        def write(*args):
            assert {fd: (os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in descriptors} == before
            return real_write(*args)

        monkeypatch.setattr(restore_io, "pending_file", write)
        verified = restore_io.materialize(
            rows, source, destination, restore.readback, filesystems, time.monotonic() + 120
        )
        assert len(verified) == len(rows)
        assert len(events) == 3
        assert {fd for event, fd in events if event == "sync"} == set(descriptors)
    assert_closed(descriptors)


def test_cursor_can_anchor_before_target_directories_exist(tmp_path, monkeypatch):
    target = tmp_path / "later" / "nested"

    class Sync:
        def __call__(self, descriptor):
            assert os.fstat(descriptor).st_ino == tmp_path.stat().st_ino
            return 0

    monkeypatch.setattr(
        restore_io.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(syncfs=Sync())
    )
    with closing(restore_io.Filesystems([target], time.monotonic() + 120)) as filesystems:
        target.mkdir(parents=True)
        filesystems.contains(target / "file")
        filesystems.sync(time.monotonic() + 120)


def test_constructor_closes_earlier_cursor_when_later_anchor_is_invalid(tmp_path, monkeypatch):
    valid, invalid = tmp_path / "valid", tmp_path / "invalid"
    valid.mkdir()
    invalid.write_bytes(b"ordinary owned fixture")
    opened = []
    real_open = restore_io.os.open

    def record_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(restore_io.os, "open", record_open)
    with pytest.raises(DeliveryError, match="anchor differs"):
        restore_io.Filesystems([valid, invalid], time.monotonic() + 120)
    assert len(opened) == 1
    assert_closed(opened)


def test_actual_two_copy_processes_join_before_return_and_preserve_all_bytes(tmp_path, monkeypatch):
    rows, source, destination = fixture_rows(
        tmp_path, [b"known first", b"known second", b"known third", b"known fourth"]
    )
    prior_children = {child.pid for child in multiprocessing.active_children()}
    markers = tmp_path / "worker-identities"
    markers.mkdir()
    original = restore_io.pending_file

    def record_worker(*args):
        (markers / str(os.getpid())).touch()
        deadline = time.monotonic() + 5
        while len(list(markers.iterdir())) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return original(*args)

    monkeypatch.setattr(restore_io, "pending_file", record_worker)
    with closing(filesystem_model(monkeypatch, destination(rows[0])[0].parent, [])) as fs:
        verified = restore_io.materialize(
            rows, source, destination, restore.readback, fs, time.monotonic() + 120
        )
    assert len(list(markers.iterdir())) == 2
    assert {child.pid for child in multiprocessing.active_children()} == prior_children
    assert all(not (restore_io.Path("/proc") / p.name).exists() for p in markers.iterdir())
    assert set(verified) == {row["path"] for row in rows}
    for row in rows:
        path, uid, gid, mode = destination(row)
        assert path.read_bytes() == source(row).read_bytes()
        restore.unchanged_file(path, verified[row["path"]], uid, gid, mode)


def test_process_copy_refuses_fork_from_a_multithreaded_owner(tmp_path, monkeypatch):
    rows, source, destination = fixture_rows(tmp_path, [b"known fixture"])
    monkeypatch.setattr(restore_io.threading, "active_count", lambda: 2)
    with closing(filesystem_model(monkeypatch, destination(rows[0])[0].parent, [])) as fs:
        with pytest.raises(DeliveryError, match="one POSIX owner thread"):
            restore_io.materialize(
                rows, source, destination, restore.readback, fs, time.monotonic() + 120
            )
    assert not destination(rows[0])[0].exists()
