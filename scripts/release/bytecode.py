"""Build checked-hash caches from trusted source; never execute cache contents."""

import importlib.util
import os
import re
import stat
import sys
from pathlib import Path

from .policy import BYTECODE_ROOTS, DeliveryError, bytecode_source, relative_name

MAX_SOURCE_BYTES = 4 * 1024**2


def _stamp(info: os.stat_result) -> tuple[int, ...]:
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


def source_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in (0o444, 0o555)
            or before.st_size > MAX_SOURCE_BYTES
        ):
            raise DeliveryError("bytecode source requires a bounded read-only ordinary file")
        data = stream.read(MAX_SOURCE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (
        len(data) != before.st_size
        or _stamp(before) != _stamp(after)
        or _stamp(after) != _stamp(path.lstat())
    ):
        raise DeliveryError("bytecode source changed during verification")
    return data


def verify_header(header: bytes, source: Path) -> None:
    # Bundle SHA-256 verification authenticates every cache byte. This extra
    # pairing binds the CPython header to the actual, separately hashed source.
    # Never deserialize or execute an incoming marshal payload to inspect it.
    if sys.version_info[:3] != (3, 13, 16):
        raise DeliveryError("bytecode verification requires the pinned delivery interpreter")
    expected = importlib.util.MAGIC_NUMBER + b"\x03\x00\x00\x00"
    if len(header) != 16 or header[:8] != expected:
        raise DeliveryError("only pinned checked-hash Python bytecode is supported")
    if header[8:] != importlib.util.source_hash(source_bytes(source)):
        raise DeliveryError("bytecode source hash differs")


def compile_payload(payload: Path, release_id: str) -> int:
    import py_compile

    if (
        sys.version_info[:3] != (3, 13, 16)
        or sys.implementation.cache_tag != "cpython-313"
        or not re.fullmatch(r"sd-[0-9a-f]{12}", release_id)
    ):
        raise DeliveryError("bytecode build requires the pinned delivery interpreter")
    sources = []
    for directory, directories, files in os.walk(payload, followlinks=False):
        directories.sort()
        if "__pycache__" in directories:
            raise DeliveryError("inherited bytecode cache refused")
        if any((Path(directory) / name).is_symlink() for name in directories):
            raise DeliveryError("bytecode build directory link refused")
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(payload).as_posix()
            if relative.startswith(BYTECODE_ROOTS) and relative.endswith(".py"):
                relative_name(relative)
                sources.append((path, relative))
                if len(sources) > 10000:
                    raise DeliveryError("bytecode source count exceeds build bound")
    for source, relative in sources:
        before = _stamp(source.lstat())
        source_bytes(source)
        # Explicit cfile prevents an inherited PYTHONPYCACHEPREFIX from sending
        # output outside this new bundle. Source filenames never expose Host paths.
        cache = source.parent / "__pycache__" / (source.stem + ".cpython-313.pyc")
        if bytecode_source(cache.relative_to(payload).as_posix()) != relative:
            raise DeliveryError("bytecode source pairing differs")
        if cache.exists() or cache.is_symlink():
            raise DeliveryError("existing bytecode cache refused")
        py_compile.compile(
            str(source),
            cfile=str(cache),
            dfile=f"/opt/kf-onlinejudge/releases/{release_id}/payload/{relative}",
            doraise=True,
            optimize=0,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        cache.chmod(0o444)
        with cache.open("rb") as stream:
            verify_header(stream.read(16), source)
        if _stamp(source.lstat()) != before:
            raise DeliveryError("source changed during bytecode compilation")
    return len(sources)
