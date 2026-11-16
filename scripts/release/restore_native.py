"""One bounded native data-copy child in the existing exclusive restore unit."""

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from kf_oj.contracts.restore_io import RestoreIOReply

from . import backup_manifest, file_copy, preflight, restore_io
from .policy import DeliveryError, invalid_constant, unique_object

MAX_REQUEST_BYTES = 256 * 1024
MAX_REPLY_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024
# Only additional list/newline framing beyond the already bounded source metadata.
MAX_SPOOL_BYTES = backup_manifest.MAX_METADATA_BYTES + 4 * backup_manifest.MAX_LOGICAL_ENTRIES


class BatchSpool:
    """Consume validated rows into private, completely verified descriptor-only input."""

    def __init__(self, rows: list[dict], directory: Path, deadline: float):
        self.deadline = deadline
        self.files = len(rows)
        self.logical_bytes = sum(row["bytes"] for row in rows)
        self.started = self.complete = False
        self.size = 0
        self.stream = None
        restore_io.remaining(deadline)
        if self.files > backup_manifest.MAX_LOGICAL_ENTRIES:
            raise DeliveryError("recovery planner inventory exceeds bound")
        try:
            self.stream = tempfile.TemporaryFile(mode="w+b", dir=directory)  # noqa: SIM115
            # Ownership spans construction, consumption and every failure path.
            info = os.fstat(self.stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 0
                or (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid())
                or stat.S_IMODE(info.st_mode) != 0o600
                or os.get_inheritable(self.stream.fileno())
            ):
                raise DeliveryError("private recovery planner descriptor required")
            expected = hashlib.sha256()
            batch = None
            for batch in restore_io.batches(rows):
                restore_io.remaining(deadline)
                raw = json.dumps(batch, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
                self.size += len(raw)
                if len(raw) > MAX_REQUEST_BYTES or self.size > MAX_SPOOL_BYTES:
                    raise DeliveryError("recovery planner input exceeds bound")
                if self.stream.write(raw) != len(raw):
                    raise DeliveryError("recovery planner write incomplete")
                expected.update(raw)
            # No row dictionary or duplicate decoded string remains here while
            # the native child materializes the full logical data inventory.
            batch = None
            rows.clear()
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.witness = file_copy._identity(os.fstat(self.stream.fileno()))
            self.digest = expected.hexdigest()
            self.stream.seek(0)
            observed = hashlib.file_digest(self.stream, "sha256").hexdigest()
            self.unchanged()
            if observed != self.digest:
                raise DeliveryError("recovery planner complete input verification failed")
            self.stream.seek(0)
            restore_io.remaining(deadline)
        except BaseException:
            self.close()
            raise

    def unchanged(self) -> None:
        if (
            self.stream is None
            or self.stream.closed
            or file_copy._identity(os.fstat(self.stream.fileno())) != self.witness
        ):
            raise DeliveryError("recovery planner descriptor changed")

    def __iter__(self) -> Iterator[list[dict]]:
        if self.started:
            raise DeliveryError("recovery planner input already consumed")
        self.started = True
        files = logical_bytes = 0
        observed = hashlib.sha256()
        assert self.stream is not None
        while True:
            restore_io.remaining(self.deadline)
            self.unchanged()
            raw = self.stream.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise DeliveryError("recovery planner input framing differs")
            observed.update(raw)
            batch = json.loads(
                raw, object_pairs_hook=unique_object, parse_constant=invalid_constant
            )
            if not isinstance(batch, list) or not 1 <= len(batch) <= restore_io.BATCH_FILES:
                raise DeliveryError("recovery planner batch shape differs")
            files += len(batch)
            logical_bytes += sum(row["bytes"] for row in batch)
            if files > self.files or logical_bytes > self.logical_bytes:
                raise DeliveryError("recovery planner inventory differs")
            yield batch
        self.unchanged()
        if (files, logical_bytes, observed.hexdigest()) != (
            self.files,
            self.logical_bytes,
            self.digest,
        ):
            raise DeliveryError("recovery planner complete input differs")
        restore_io.remaining(self.deadline)
        self.complete = True

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _traceback):
        try:
            if kind is None and not self.complete:
                raise DeliveryError("recovery planner input incomplete")
        finally:
            self.close()


class Session:
    """No fallback after partial writes; all verified witnesses stay in the child."""

    def __init__(
        self,
        payload: Path,
        backup_id: str,
        recovery_id: str,
        expected_files: int,
        expected_bytes: int,
        deadline: float,
    ):
        self.deadline = deadline
        self.batch_id = self.files = self.bytes = 0
        self.expected_files, self.expected_bytes = expected_files, expected_bytes
        self.finished = False
        self.process = None
        self.stderr_bytes = 0
        helper = payload / "bin/kf-restore-io"
        preflight.protected(helper)
        info = helper.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o555
        ):
            raise DeliveryError("selected native recovery helper differs")
        witness = file_copy._identity(info)
        self.selector = selectors.DefaultSelector()
        try:
            restore_io.remaining(self.deadline)
            self.process = subprocess.Popen(
                [str(helper), "--stdio-v1"],
                cwd="/",
                env={
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C.UTF-8",
                    "LD_LIBRARY_PATH": f"{payload}/lib:{payload}/python/lib",
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
            if file_copy._identity(helper.lstat()) != witness:
                raise DeliveryError("selected native recovery helper changed")
            assert self.process.stdin and self.process.stdout and self.process.stderr
            for handle in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                os.set_blocking(handle.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
            self._exchange(
                {
                    "schema_version": 1,
                    "kind": "restore.io.open",
                    "backup_id": backup_id,
                    "recovery_id": recovery_id,
                    "expected_files": expected_files,
                    "expected_logical_bytes": expected_bytes,
                    "deadline_monotonic_ns": int(deadline * 1_000_000_000),
                },
                "opened",
            )
        except BaseException:
            self.close()
            raise

    def _exchange(self, value: dict, stage: str) -> None:
        restore_io.remaining(self.deadline)
        if self.process is None or self.finished:
            raise DeliveryError("native recovery session is closed")
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        if len(raw) > MAX_REQUEST_BYTES:
            raise DeliveryError("native recovery request exceeds bound")
        assert self.process.stdin
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        offset = 0
        output = bytearray()
        try:
            while True:
                restore_io.remaining(self.deadline)
                ready = self.selector.select(max(0, self.deadline - time.monotonic()))
                if not ready:
                    raise DeliveryError("native recovery copy deadline exceeded")
                for key, _ in ready:
                    if key.data == "stdin":
                        try:
                            count = os.write(key.fd, memoryview(raw)[offset:])
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise DeliveryError("native recovery request pipe closed")
                        offset += count
                        if offset == len(raw):
                            self.selector.unregister(self.process.stdin)
                        continue
                    try:
                        block = os.read(key.fd, 8192)
                    except BlockingIOError:
                        continue
                    if key.data == "stderr":
                        self.stderr_bytes += len(block)
                        if self.stderr_bytes > MAX_STDERR_BYTES:
                            raise DeliveryError("native recovery diagnostic exceeds bound")
                        if not block:
                            self.selector.unregister(key.fileobj)
                        continue
                    if not block:
                        raise DeliveryError("native recovery stopped before verification")
                    output.extend(block)
                    if len(output) > MAX_REPLY_BYTES:
                        raise DeliveryError("native recovery reply exceeds bound")
                    if b"\n" in output:
                        if offset != len(raw) or output[-1:] != b"\n" or output.count(b"\n") != 1:
                            raise DeliveryError("native recovery reply framing differs")
                        reply = RestoreIOReply.model_validate(
                            json.loads(
                                output,
                                object_pairs_hook=unique_object,
                                parse_constant=invalid_constant,
                            )
                        )
                        if (
                            reply.stage != stage
                            or reply.error != "none"
                            or (
                                reply.batch_id,
                                reply.verified_files,
                                reply.verified_logical_bytes,
                            )
                            != (self.batch_id, self.files, self.bytes)
                        ):
                            raise DeliveryError("native recovery file verification refused")
                        restore_io.remaining(self.deadline)
                        return
        except (OSError, ValueError, RecursionError) as error:
            raise DeliveryError("native recovery transport or reply differs") from error
        finally:
            if self.process.stdin in self.selector.get_map():
                self.selector.unregister(self.process.stdin)

    def materialize(self, rows: list[dict]) -> None:
        self._materialize(restore_io.batches(rows))

    def materialize_owned(self, rows: list[dict], directory: Path) -> None:
        with BatchSpool(rows, directory, self.deadline) as batches:
            self._materialize(iter(batches))

    def _materialize(self, batches: Iterator[list[dict]]) -> None:
        for batch in batches:
            self.batch_id += 1
            self.files += len(batch)
            self.bytes += sum(item["bytes"] for item in batch)
            if self.files > self.expected_files or self.bytes > self.expected_bytes:
                raise DeliveryError("native recovery inventory exceeds original receipt")
            # The complete private manifest was already strictly validated.
            # The native decoder independently validates each row before use.
            self._exchange(
                {
                    "schema_version": 1,
                    "kind": "restore.io.batch",
                    "batch_id": self.batch_id,
                    "files": batch,
                },
                "verified",
            )

    def finish(self) -> None:
        if (self.files, self.bytes) != (self.expected_files, self.expected_bytes):
            raise DeliveryError("native recovery inventory incomplete")
        self._exchange({"schema_version": 1, "kind": "restore.io.finish"}, "finished")
        assert self.process is not None and self.process.stdin
        self.process.stdin.close()
        restore_io.remaining(self.deadline)
        try:
            code = self.process.wait(timeout=max(0, self.deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise DeliveryError("native recovery child did not join") from error
        if code != 0:
            raise DeliveryError("native recovery child failed after verification")
        restore_io.remaining(self.deadline)
        self.finished = True

    def close(self) -> None:
        process = self.process
        try:
            if process is not None and process.poll() is None:
                # This child starts its own session and never spawns processes.
                # Its only descendants are its two native threads.
                try:
                    if os.getpgid(process.pid) != process.pid:
                        raise DeliveryError("native recovery child ownership differs")
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)
        finally:
            self.selector.close()
            if process is not None:
                for handle in (process.stdin, process.stdout, process.stderr):
                    if handle is not None:
                        handle.close()
