"""Fixed-size regular-file transfer; callers retain durability and byte readback."""

import errno
import os
import stat
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from typing import BinaryIO, cast

from .policy import MAX_FILE_BYTES, DeliveryError

CHUNK = 1024 * 1024
UNSUPPORTED = {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}
IN_FLIGHT = 4


def bounded_copies[Item, Result](
    rows: Sequence[Item],
    operation: Callable[[Item], Result],
    *,
    deadline: float | None = None,
) -> list[Result]:
    """Join a finite verified inventory, with at most four operations in flight."""

    def remaining() -> float | None:
        if deadline is None:
            return None
        value = deadline - time.monotonic()
        if value <= 0:
            raise DeliveryError("copy deadline exceeded; private partial target retained")
        return value

    remaining()
    if len(rows) < 2:
        single = [operation(row) for row in rows]
        remaining()
        return single
    results: list[Result | None] = [None] * len(rows)
    source = iter(enumerate(rows))
    lock = threading.Lock()
    stopped = False

    def worker() -> None:
        nonlocal stopped
        try:
            while True:
                with lock:
                    if stopped:
                        return
                    remaining()
                    following = next(source, None)
                    if following is None:
                        return
                    index, row = following
                result = operation(row)
                with lock:
                    results[index] = result
        except BaseException:
            # Stop dispatch before this future publishes failure. Running calls
            # are still joined; their partial results cannot become a receipt.
            with lock:
                stopped = True
            raise

    # One future per worker, not per file. The locked iterator still admits
    # only four individual operations, without an eagerly queued inventory.
    count = min(IN_FLIGHT, len(rows))
    with ThreadPoolExecutor(max_workers=count) as executor:
        pending = []
        try:
            for _ in range(count):
                pending.append(executor.submit(worker))
            ready, incomplete = wait(pending, timeout=remaining(), return_when=FIRST_EXCEPTION)
            for future in ready:
                future.result()
            if incomplete:
                raise DeliveryError("copy deadline exceeded; private partial target retained")
            remaining()
        finally:
            with lock:
                stopped = True
            for future in pending:
                future.cancel()
            # The executor joins every running call even after cancellation or
            # deadline expiry. No success receipt can outrun file I/O.

    return cast(list[Result], results)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def transfer(
    incoming: BinaryIO, outgoing: BinaryIO, expected_bytes: int, *, deadline: float | None = None
) -> None:
    """Use fresh streams; unsupported kernel transfer resumes at the same offsets."""
    source, target = incoming.fileno(), outgoing.fileno()
    before = os.fstat(source)
    destination = os.fstat(target)
    if (
        type(expected_bytes) is not int
        or not 0 <= expected_bytes <= MAX_FILE_BYTES
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != expected_bytes
        or not stat.S_ISREG(destination.st_mode)
        or destination.st_nlink != 1
        or destination.st_size != 0
        or incoming.tell() != 0
        or outgoing.tell() != 0
        or os.lseek(source, 0, os.SEEK_CUR) != 0
        or os.lseek(target, 0, os.SEEK_CUR) != 0
    ):
        raise DeliveryError("fixed-size regular copy input or fresh destination differs")

    def remaining() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise DeliveryError("copy deadline exceeded; private partial target retained")

    total = 0
    kernel = hasattr(os, "sendfile")
    while total < expected_bytes:
        remaining()
        maximum = min(CHUNK, expected_bytes - total)
        if kernel:
            try:
                # Linux None advances the source offset, including a copied prefix
                # before falling back. Each call stays within the verified size.
                count = os.sendfile(target, source, None, maximum)
            except OSError as error:
                if error.errno not in UNSUPPORTED:
                    raise
                kernel = False
                continue
        else:
            data = incoming.read(maximum)
            count = len(data)
            if count and outgoing.write(data) != count:
                raise DeliveryError("short copy write; private partial target retained")
        if not 0 < count <= maximum:
            raise DeliveryError("copy input bytes differ; private partial target retained")
        total += count
    remaining()
    if incoming.read(1) or _identity(os.fstat(source)) != _identity(before):
        raise DeliveryError("copy input changed; private partial target retained")
