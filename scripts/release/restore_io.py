"""Bounded cold-copy batches, durable before full destination byte readback."""

import ctypes
import multiprocessing
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import file_copy
from .policy import DeliveryError

BATCH_FILES = 256
BATCH_BYTES = 64 * 1024**2
COPY_PROCESSES = 2


def remaining(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise DeliveryError("recovery copy deadline exceeded; private partial target retained")


def batches(rows: Sequence[dict]) -> Iterator[list[dict]]:
    """Keep repeated objects adjacent; large files have an exclusive batch."""
    current: list[dict] = []
    size = 0
    for row in sorted(rows, key=lambda item: (item["sha256"], item["path"])):
        count = row["bytes"]
        if current and (len(current) >= BATCH_FILES or size + count > BATCH_BYTES):
            yield current
            current, size = [], 0
        current.append(row)
        size += count
        if size >= BATCH_BYTES:
            yield current
            current, size = [], 0
    if current:
        yield current


class Filesystems:
    """Hold filesystem error cursors from before the first destination write."""

    def __init__(self, paths: Sequence[Path], deadline: float | None):
        self.handles: dict[int, tuple[Path, int, int]] = {}
        self.closed = False
        remaining(deadline)
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            self._syncfs = libc.syncfs
        except AttributeError as error:
            raise DeliveryError("recovery filesystem synchronization unavailable") from error
        self._syncfs.argtypes, self._syncfs.restype = [ctypes.c_int], ctypes.c_int
        try:
            for path in paths:
                remaining(deadline)
                while not path.exists() and not path.is_symlink():
                    if path.parent == path:
                        raise DeliveryError("recovery filesystem ancestor unavailable")
                    path = path.parent
                before = path.lstat()
                if not stat.S_ISDIR(before.st_mode):
                    raise DeliveryError("recovery filesystem anchor differs")
                if before.st_dev in self.handles:
                    continue
                descriptor = os.open(
                    path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                try:
                    if file_copy._identity(os.fstat(descriptor)) != file_copy._identity(before):
                        raise DeliveryError("recovery filesystem anchor changed")
                except BaseException:
                    os.close(descriptor)
                    raise
                self.handles[before.st_dev] = (path, descriptor, before.st_ino)
        except BaseException:
            self.close()
            raise

    def contains(self, path: Path) -> None:
        parent = path.parent.lstat()
        if self.closed or not stat.S_ISDIR(parent.st_mode) or parent.st_dev not in self.handles:
            raise DeliveryError("recovery destination has no pre-write filesystem cursor")

    def sync(self, deadline: float | None) -> None:
        if self.closed:
            raise DeliveryError("recovery filesystem cursor is closed")
        for device, (path, descriptor, inode) in self.handles.items():
            remaining(deadline)
            current, opened = path.lstat(), os.fstat(descriptor)
            # This operator legitimately changes directory entries and modes.
            # The retained descriptor must still refer to the same directory.
            if any(
                not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode)
                for info in (current, opened)
            ):
                raise DeliveryError("recovery filesystem anchor changed")
            if self._syncfs(descriptor) != 0:
                raise DeliveryError("recovery filesystem synchronization failed")
            remaining(deadline)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            error = None
            for _, descriptor, _ in self.handles.values():
                try:
                    os.close(descriptor)
                except OSError as failure:
                    error = failure
            if error is not None:
                raise error


def pending_file(
    source: Path, path: Path, item: dict, uid: int, gid: int, mode: int, deadline: float
) -> tuple[int, ...]:
    """Materialize only; this witness cannot certify bytes before batch readback."""
    remaining(deadline)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_size)
            != (uid, gid, mode, item["bytes"])
        ):
            raise DeliveryError("existing recovery destination differs; no overwrite")
        return file_copy._identity(info)
    with (
        os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as incoming,
        os.fdopen(
            os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            ),
            "wb",
        ) as outgoing,
    ):
        file_copy.transfer(incoming, outgoing, item["bytes"], deadline=deadline)
        outgoing.flush()
        os.fchown(outgoing.fileno(), uid, gid)
        os.fchmod(outgoing.fileno(), mode)
        witness = file_copy._identity(os.fstat(outgoing.fileno()))
    if file_copy._identity(path.lstat()) != witness:
        raise DeliveryError("recovery destination changed before synchronization")
    return witness


def _copy_group(group: list[tuple], deadline: float) -> list[tuple[int, ...]]:
    return [
        pending_file(source, path, item, uid, gid, mode, deadline)
        for item, source, path, uid, gid, mode in group
    ]


def _read_group(readback: Callable, group: list[tuple], deadline: float) -> list[tuple[int, ...]]:
    return [
        readback(path, item, uid, gid, mode, deadline) for item, path, uid, gid, mode, _ in group
    ]


def materialize(
    rows: Sequence[dict],
    source: Callable[[dict], Path],
    destination: Callable[[dict], tuple[Path, int, int, int]],
    readback: Callable[[Path, dict, int, int, int, float], tuple[int, ...]],
    filesystems: Filesystems,
    deadline: float,
    *,
    group_readback: Callable[[list[tuple], float], list[tuple[int, ...]]] | None = None,
) -> dict[str, tuple[int, ...]]:
    """Copy, join, sync and independently read each complete destination.

    Two joined child processes use the existing exclusive node CPU ceiling;
    no private service/cgroup or extra resource budget is introduced. Each
    worker processes a bounded half-batch without one Future per file. The
    parent retains the pre-write error cursors and flushes only after both
    copy groups join, then dispatches full byte readback. A batch retains at
    most 256 file records and 64 MiB; a larger file is handled alone.
    Every returned witness follows an actual filesystem flush and complete
    destination byte verification. The optional grouped reader can reuse an
    actually hashed immutable reference only after full byte equality readback
    of each remaining target. Exceptions join outstanding work and retain partial files.
    """
    remaining(deadline)
    if os.name != "posix" or threading.active_count() != 1:
        raise DeliveryError("recovery process workers require one POSIX owner thread")
    verified = {}
    executor = ProcessPoolExecutor(
        max_workers=COPY_PROCESSES, mp_context=multiprocessing.get_context("fork")
    )
    # Shutdown joins every child on either success or failure. The fixed unit
    # deadline additionally bounds a blocked syscall or unresponsive child.
    with executor:
        for batch in batches(rows):
            pending = []
            remaining(deadline)
            group_size = max(1, (len(batch) + COPY_PROCESSES - 1) // COPY_PROCESSES)
            groups = [
                [
                    (item, source(item), *destination(item))
                    for item in batch[offset : offset + group_size]
                ]
                for offset in range(0, len(batch), group_size)
            ]
            for group in groups:
                for _, _, path, _, _, _ in group:
                    filesystems.contains(path)
            calls = [executor.submit(_copy_group, group, deadline) for group in groups]
            for group, call in zip(groups, calls, strict=True):
                for (item, _, path, uid, gid, mode), witness in zip(
                    group, call.result(), strict=True
                ):
                    pending.append((item, path, uid, gid, mode, witness))
            filesystems.sync(deadline)
            groups = [
                pending[offset : offset + group_size]
                for offset in range(0, len(pending), group_size)
            ]
            calls = [
                executor.submit(group_readback, group, deadline)
                if group_readback is not None
                else executor.submit(_read_group, readback, group, deadline)
                for group in groups
            ]
            for group, call in zip(groups, calls, strict=True):
                for (item, _, _, _, _, before), actual in zip(group, call.result(), strict=True):
                    if actual != before:
                        raise DeliveryError("recovery destination changed across synchronization")
                    verified[item["path"]] = actual
    remaining(deadline)
    return verified
