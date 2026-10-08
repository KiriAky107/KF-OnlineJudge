"""Private immutable byte store. Authorization is applied before calling this adapter."""

import fcntl
import hashlib
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from kf_oj.contracts.judging import MAX_PACKAGE_BYTES, ArtifactReference

DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TEMP = re.compile(r"\.tmp-[0-9a-f]{32}\Z")


class ArtifactError(ValueError):
    """Sanitized artifact failure; never includes paths or private byte content."""


class ArtifactStore:
    def __init__(self, root: Path, *, max_bytes: int = 512 * 1024 * 1024, max_files: int = 4096):
        if not 1 <= max_bytes <= 16 * 1024**3 or not 1 <= max_files <= 65536:
            raise ArtifactError("invalid storage capacity")
        root = root.absolute()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.resolve() != root:
            raise ArtifactError("storage root must not contain links")
        self._directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.max_bytes = max_bytes
        self.max_files = max_files
        info = os.fstat(self._directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(self._directory)
            raise ArtifactError("storage root must be private and owned")
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._directory)
            self._closed = True

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        # Open a different description for every operation: flock also serializes threads.
        descriptor = os.open(
            ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._directory,
        )
        try:
            self._regular(os.fstat(descriptor))
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ArtifactError("storage lock deadline exceeded") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _regular(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ArtifactError("invalid storage entry")

    def _entries(self) -> list[tuple[str, os.stat_result]]:
        entries = []
        with os.scandir(self._directory) as directory:
            for entry in directory:
                if entry.name == ".lock":
                    continue
                if not DIGEST.fullmatch(entry.name) and not TEMP.fullmatch(entry.name):
                    raise ArtifactError("unexpected storage entry")
                info = entry.stat(follow_symlinks=False)
                self._regular(info)
                entries.append((entry.name, info))
                if len(entries) > self.max_files:
                    raise ArtifactError("storage file capacity exceeded")
        return entries

    @staticmethod
    def reference(data: bytes) -> ArtifactReference:
        if type(data) is not bytes or len(data) > MAX_PACKAGE_BYTES:
            raise ArtifactError("artifact size outside policy")
        return ArtifactReference(digest=hashlib.sha256(data).hexdigest(), bytes=len(data))

    def put(self, data: bytes, expected: ArtifactReference | None = None) -> ArtifactReference:
        reference = self.reference(data)
        if expected is not None and reference != expected:
            raise ArtifactError("artifact digest or length mismatch")
        with self.exclusive():
            entries = self._entries()
            if any(name == reference.digest for name, _ in entries):
                self._read(reference)
                return reference
            if len(entries) + 1 > self.max_files or (
                sum(info.st_size for _, info in entries) + len(data) > self.max_bytes
            ):
                raise ArtifactError("storage capacity exhausted")
            temporary = ".tmp-" + uuid4().hex
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                # Locking covers every mutation. Never replace or repair an existing digest.
                os.rename(
                    temporary,
                    reference.digest,
                    src_dir_fd=self._directory,
                    dst_dir_fd=self._directory,
                )
                os.fsync(self._directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=self._directory)
                except FileNotFoundError:
                    pass
        return reference

    def _read(self, reference: ArtifactReference) -> bytes:
        if (
            type(reference.digest) is not str
            or not DIGEST.fullmatch(reference.digest)
            or type(reference.bytes) is not int
            or not 0 <= reference.bytes <= MAX_PACKAGE_BYTES
        ):
            raise ArtifactError("invalid artifact reference")
        try:
            descriptor = os.open(
                reference.digest,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self._directory,
            )
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                self._regular(info)
                if info.st_size != reference.bytes:
                    raise ArtifactError("artifact digest or length mismatch")
                data = source.read(reference.bytes + 1)
                if self.reference(data) != reference:
                    raise ArtifactError("artifact digest or length mismatch")
                return data
        except OSError:
            raise ArtifactError("artifact unavailable") from None

    def read(self, reference: ArtifactReference) -> bytes:
        with self.exclusive():
            return self._read(reference)

    def remove(self, digest: str) -> None:
        if not DIGEST.fullmatch(digest):
            raise ArtifactError("invalid artifact identifier")
        with self.exclusive():
            try:
                info = os.stat(digest, dir_fd=self._directory, follow_symlinks=False)
                self._regular(info)
                os.unlink(digest, dir_fd=self._directory)
                os.fsync(self._directory)
            except FileNotFoundError:
                pass

    def old_digests(self, *, min_age_seconds: int, limit: int) -> tuple[str, ...]:
        if not 3600 <= min_age_seconds or not 1 <= limit <= 100:
            raise ArtifactError("invalid collection budget")
        with self.exclusive():
            cutoff = time.time() - min_age_seconds
            return tuple(
                name
                for name, info in self._entries()
                if DIGEST.fullmatch(name) and info.st_mtime < cutoff
            )[:limit]

    def clean_temporary(self, *, min_age_seconds: int = 86400, limit: int = 100) -> int:
        if min_age_seconds < 3600 or not 1 <= limit <= 100:
            raise ArtifactError("invalid collection budget")
        with self.exclusive():
            cutoff = time.time() - min_age_seconds
            names = [
                name
                for name, info in self._entries()
                if TEMP.fullmatch(name) and info.st_mtime < cutoff
            ][:limit]
            for name in names:
                os.unlink(name, dir_fd=self._directory)
            if names:
                os.fsync(self._directory)
            return len(names)
