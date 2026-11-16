"""Private content storage; every logical source byte is read before deduplication."""

import ctypes
import errno
import hashlib
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

from . import file_copy
from .policy import DeliveryError, sha256

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


@dataclass(frozen=True)
class _SourceHash:
    path: str
    original_identity: tuple[int, ...]
    digest: str


def hash_source(source: Path, before, deadline: float) -> _SourceHash:
    """Read the complete source on a bounded worker; publish no backup object.

    The owner checks this same identity again before storage. New objects are
    still copied and independently hashed; every final object is independently
    verified before the public receipt. Source pages remain available until the
    owner finishes that file, within the unchanged service memory limit.
    """
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 <= before.st_size <= 256 * 1024**2
    ):
        raise DeliveryError("special, linked or oversized backup source refused")
    with os.fdopen(
        os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb"
    ) as incoming:
        digest = read_stream(incoming, before, deadline)
    if identity(source.lstat()) != identity(before):
        raise DeliveryError("backup source path changed during hashing")
    return _SourceHash(str(source), identity(before), digest)


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


MIN_COMPARISON_BYTES = 64 * 1024
MAX_COMPARISON_HINTS = 4096


def same_object_bytes(incoming, before, path: Path, deadline: float) -> bool:
    """Read both whole files; a hint never establishes content identity.

    The object digest was computed during this same capture. Successful equality
    transfers that digest only after every byte matches. The final independent
    full-object SHA verification still precedes the public capture receipt.
    """
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        != (os.geteuid(), os.getegid(), 0o600)
        or info.st_size != before.st_size
        or (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino)
    ):
        raise DeliveryError("private backup comparison object protection differs")
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb"
    ) as existing:
        if identity(os.fstat(existing.fileno())) != identity(info) or identity(
            os.fstat(incoming.fileno())
        ) != identity(before):
            raise DeliveryError("backup comparison input changed before read")
        count, equal = 0, True
        while block := incoming.read(CHUNK):
            count += len(block)
            if count > before.st_size or time.monotonic() >= deadline:
                raise DeliveryError("backup comparison source or deadline changed")
            if block != existing.read(len(block)):
                equal = False
                break
        if equal and (count != before.st_size or existing.read(1)):
            raise DeliveryError("backup comparison byte count differs")
        if (
            time.monotonic() >= deadline
            or identity(os.fstat(incoming.fileno())) != identity(before)
            or identity(os.fstat(existing.fileno())) != identity(info)
            or identity(path.lstat()) != identity(info)
        ):
            raise DeliveryError("backup comparison input changed during read")
        discard_consumed_cache(existing, info.st_size)
    return equal


def store(
    source: Path,
    before,
    objects: Path,
    seen: dict[str, int],
    deadline: float,
    *,
    remaining_bytes: int,
    candidates: dict[tuple[str, int], str] | None = None,
    _prehashed: _SourceHash | None = None,
    _captured: dict[str, tuple[int, ...]] | None = None,
) -> tuple[str, int]:
    """Read every source; reuse only a verified current capture content digest.

    The one fixed pending file is exclusive. An interrupted copy remains private
    and prevents adoption of the incomplete snapshot. Publishing never replaces
    an existing object, and the final object has exactly one link.
    """
    if (_prehashed is None) != (_captured is None):
        raise DeliveryError("incomplete current capture hashing context")
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DeliveryError("special or linked backup source refused")
    with os.fdopen(
        os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC),
        "rb",
    ) as incoming:
        key = (source.name, before.st_size)
        if _prehashed is not None:
            if (
                type(_prehashed) is not _SourceHash
                or _prehashed.path != str(source)
                or _prehashed.original_identity != identity(before)
                or identity(os.fstat(incoming.fileno())) != identity(before)
                or identity(source.lstat()) != identity(before)
                or time.monotonic() >= deadline
            ):
                raise DeliveryError("backup hashed source identity or deadline changed")
            digest = sha256(_prehashed.digest)
        else:
            # File name and size choose at most one candidate; neither is a proof.
            # Hints live only in this invocation and never avoid a complete byte read.
            key = (source.name, before.st_size)
            candidate = (
                candidates.get(key)
                if candidates is not None and before.st_size >= MIN_COMPARISON_BYTES
                else None
            )
            matched = False
            if candidate is not None:
                sha256(candidate)
                if seen.get(candidate) != before.st_size:
                    raise DeliveryError("backup comparison hint has no current captured object")
                matched = same_object_bytes(incoming, before, objects / candidate, deadline)
            if matched:
                assert candidate is not None
                digest = candidate
            else:
                incoming.seek(0)
                digest = read_stream(incoming, before, deadline)
                if candidate == digest:
                    raise DeliveryError("private backup object differs from its captured digest")
        added = 0
        if digest in seen:
            if seen[digest] != before.st_size:
                raise DeliveryError("backup content size conflict")
            if _captured is not None:
                existing = (objects / digest).lstat()
                if (
                    not stat.S_ISREG(existing.st_mode)
                    or existing.st_nlink != 1
                    or (existing.st_uid, existing.st_gid, stat.S_IMODE(existing.st_mode))
                    != (os.geteuid(), os.getegid(), 0o600)
                    or existing.st_size != before.st_size
                    or _captured.get(digest) != identity(existing)
                ):
                    raise DeliveryError("private backup object changed after capture")
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
            if _captured is not None:
                _captured[digest] = identity((objects / digest).lstat())
        if (
            candidates is not None
            and before.st_size >= MIN_COMPARISON_BYTES
            and (key in candidates or len(candidates) < MAX_COMPARISON_HINTS)
        ):
            candidates[key] = digest
        discard_consumed_cache(incoming, before.st_size)
        if identity(source.lstat()) != identity(before):
            raise DeliveryError("backup source path changed during capture")
        return digest, added


def store_batch(
    rows: list[tuple[Path, os.stat_result]],
    objects: Path,
    seen: dict[str, int],
    captured: dict[str, tuple[int, ...]],
    executor: ThreadPoolExecutor,
    deadline: float,
    *,
    remaining_bytes: int,
) -> list[tuple[str, int]]:
    """Read at most four whole sources, then publish serially with one owner.

    The caller owns a single fixed four-thread executor and joins it on every
    exit. No object is stored until every source hash in this batch succeeds.
    The exclusive pending file, physical limit, source identities and final
    independent object verification remain mandatory.
    """
    if not 1 <= len(rows) <= file_copy.IN_FLIGHT == 4:
        raise DeliveryError("backup hashing concurrency exceeds fixed bound")
    futures = []
    try:
        for path, before in rows:
            futures.append(executor.submit(hash_source, path, before, deadline))
        hashed = []
        for future in futures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryError("private backup hashing deadline")
            try:
                hashed.append(future.result(timeout=remaining))
            except FutureTimeout as error:
                raise DeliveryError("private backup hashing deadline") from error
    finally:
        for future in futures:
            future.cancel()
    results = []
    for (path, before), fingerprint in zip(rows, hashed, strict=True):
        free = os.statvfs(objects)
        if free.f_frsize * free.f_bavail < before.st_size + 64 * 1024**2:
            raise DeliveryError("private backup capacity exhausted")
        digest, added = store(
            path,
            before,
            objects,
            seen,
            deadline,
            remaining_bytes=remaining_bytes,
            _prehashed=fingerprint,
            _captured=captured,
        )
        remaining_bytes -= added
        results.append((digest, added))
    return results


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
