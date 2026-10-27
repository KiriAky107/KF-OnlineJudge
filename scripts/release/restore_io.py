"""Bounded cold-copy batches, durable before full destination byte readback."""

import ctypes
import os
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import file_copy
from .policy import DeliveryError

BATCH_FILES = 256
BATCH_BYTES = 64 * 1024**2


def remaining(deadline: float) -> None:
    if time.monotonic() >= deadline:
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

    def __init__(self, paths: Sequence[Path], deadline: float):
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

    def sync(self, deadline: float) -> None:
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


def materialize(
    rows: Sequence[dict],
    source: Callable[[dict], Path],
    destination: Callable[[dict], tuple[Path, int, int, int]],
    readback: Callable[[Path, dict, int, int, int, float], tuple[int, ...]],
    filesystems: Filesystems,
    deadline: float,
) -> dict[str, tuple[int, ...]]:
    """Copy, join, sync and independently read each complete destination.

    Only four calls are dispatched at a time. A batch retains at most 256 file
    records and 64 MiB of logical contents; a larger file is handled alone.
    Every returned witness follows an actual filesystem flush and full SHA256
    destination read. Exceptions join outstanding work and retain partial files.
    """
    remaining(deadline)
    verified = {}
    with ThreadPoolExecutor(max_workers=file_copy.IN_FLIGHT) as executor:
        for batch in batches(rows):
            pending = []
            for offset in range(0, len(batch), file_copy.IN_FLIGHT):
                remaining(deadline)
                group = [
                    (item, *destination(item))
                    for item in batch[offset : offset + file_copy.IN_FLIGHT]
                ]
                for _, path, _, _, _ in group:
                    filesystems.contains(path)
                calls = [
                    executor.submit(
                        pending_file, source(item), path, item, uid, gid, mode, deadline
                    )
                    for item, path, uid, gid, mode in group
                ]
                for record, call in zip(group, calls, strict=True):
                    pending.append((*record, call.result()))
            filesystems.sync(deadline)
            for offset in range(0, len(pending), file_copy.IN_FLIGHT):
                remaining(deadline)
                group = pending[offset : offset + file_copy.IN_FLIGHT]
                calls = [
                    executor.submit(readback, path, item, uid, gid, mode, deadline)
                    for item, path, uid, gid, mode, _ in group
                ]
                for (item, _, _, _, _, before), call in zip(group, calls, strict=True):
                    actual = call.result()
                    if actual != before:
                        raise DeliveryError("recovery destination changed across synchronization")
                    verified[item["path"]] = actual
    remaining(deadline)
    return verified
