"""Private content storage; every logical source byte is read before deduplication."""

import ctypes
import errno
import hashlib
import os
import stat
import time
from pathlib import Path

from . import file_copy
from .policy import DeliveryError

CHUNK = 1024 * 1024


def identity(info: os.stat_result) -> tuple[int, ...]:
    return file_copy._identity(info)


def read_stream(incoming, before, deadline: float, outgoing=None) -> str:
    if identity(os.fstat(incoming.fileno())) != identity(before):
        raise DeliveryError("backup source changed before capture")
    count, digest = 0, hashlib.sha256()
    while block := incoming.read(CHUNK):
        count += len(block)
        if count > before.st_size or time.monotonic() >= deadline:
            raise DeliveryError("backup source or deadline changed")
        digest.update(block)
        if outgoing is not None:
            outgoing.write(block)
    if (
        count != before.st_size
        or time.monotonic() >= deadline
        or identity(os.fstat(incoming.fileno())) != identity(before)
    ):
        raise DeliveryError("backup source changed during capture")
    return digest.hexdigest()


def discard_consumed_cache(stream, size: int) -> None:
    """Release sequentially consumed pages without changing capture or durability.

    Some filesystems do not implement cache advice. Only that unsupported case
    is optional; other I/O errors still stop capture. The final syncfs remains
    responsible for durable data and metadata before manifest publication.
    """
    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
        return
    try:
        os.posix_fadvise(stream.fileno(), 0, size, os.POSIX_FADV_DONTNEED)
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise


def store(
    source: Path,
    before,
    objects: Path,
    seen: dict[str, int],
    deadline: float,
    *,
    remaining_bytes: int,
) -> tuple[str, int]:
    """Hash every source; copy only new content, with a second full byte check.

    The one fixed pending file is exclusive. An interrupted copy remains private
    and prevents adoption of the incomplete snapshot. Publishing never replaces
    an existing object, and the final object has exactly one link.
    """
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DeliveryError("special or linked backup source refused")
    with os.fdopen(
        os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC),
        "rb",
    ) as incoming:
        digest = read_stream(incoming, before, deadline)
        added = 0
        if digest in seen:
            if seen[digest] != before.st_size:
                raise DeliveryError("backup content size conflict")
        else:
            if before.st_size > remaining_bytes:
                raise DeliveryError("private backup physical capacity exhausted")
            pending = objects / ".pending"
            with os.fdopen(
                os.open(
                    pending,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                ),
                "wb",
            ) as outgoing:
                os.fchmod(outgoing.fileno(), 0o600)
                incoming.seek(0)
                if read_stream(incoming, before, deadline, outgoing) != digest:
                    raise DeliveryError("backup source bytes changed between reads")
                outgoing.flush()
                discard_consumed_cache(outgoing, before.st_size)
                written = os.fstat(outgoing.fileno())
            if identity(pending.lstat()) != identity(written):
                raise DeliveryError("private backup pending object changed")
            os.link(pending, objects / digest, follow_symlinks=False)
            # Only this operation's completed, exclusively created staging link.
            pending.unlink()
            seen[digest] = before.st_size
            added = before.st_size
        discard_consumed_cache(incoming, before.st_size)
        if identity(source.lstat()) != identity(before):
            raise DeliveryError("backup source path changed during capture")
        return digest, added


def sync_filesystem(directory: Path, deadline: float) -> None:
    """Flush the dedicated backup filesystem before publishing its manifest.

    Linux syncfs reports delayed write errors and waits for data and metadata.
    The existing service deadline bounds a blocked filesystem operation too.
    """
    if time.monotonic() >= deadline:
        raise DeliveryError("private backup capture deadline before synchronization")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            syncfs = libc.syncfs
        except AttributeError as error:
            raise DeliveryError("private backup filesystem synchronization unavailable") from error
        syncfs.argtypes, syncfs.restype = [ctypes.c_int], ctypes.c_int
        if syncfs(descriptor) != 0:
            raise DeliveryError("private backup filesystem synchronization failed")
        if time.monotonic() >= deadline:
            raise DeliveryError("private backup capture deadline after synchronization")
    finally:
        os.close(descriptor)
