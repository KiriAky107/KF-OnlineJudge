"""One bounded native data-copy child in the existing exclusive restore unit."""

import json
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path

from kf_oj.contracts.restore_io import RestoreIOReply

from . import file_copy, preflight, restore_io
from .policy import DeliveryError, invalid_constant, unique_object

MAX_REQUEST_BYTES = 256 * 1024
MAX_REPLY_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024


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
            for handle in (self.process.stdin, self.process.stdout, self.process.stderr):
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
                            or (reply.batch_id, reply.verified_files, reply.verified_logical_bytes)
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
        for batch in restore_io.batches(rows):
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
