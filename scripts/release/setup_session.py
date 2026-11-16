"""Finite operator automation using the existing setup CLI entry points."""

import contextlib
import hashlib
import importlib
import io
import os
import signal
import threading
import time
from dataclasses import dataclass

from . import manifest
from .policy import DeliveryError

_ENTRIES = {
    "scripts.release": "scripts.release.__main__",
    **{
        "scripts.release." + name: "scripts.release." + name
        for name in (
            "journal",
            "dependencies",
            "dependency_install",
            "restore",
            "services",
            "service_install",
            "factory_service",
            "proxy_install",
        )
    },
}
_DELIVERY_COMMANDS = {
    "verify",
    "preflight",
    "install-files",
    "generate-configuration",
    "install-configuration",
    "activate",
    "generate-proxy",
}


def _require_operator() -> None:
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (
        0,
        0,
        0,
        0,
    ) or not set(os.getgroups()) <= {0}:
        raise DeliveryError("setup session requires the trusted root operator")


class _Output(io.StringIO):
    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum
        self.bytes_written = 0

    def write(self, value: str) -> int:
        size = len(value.encode("utf-8"))
        if self.bytes_written + size > self.maximum:
            raise DeliveryError("setup CLI output exceeds bound")
        self.bytes_written += size
        return super().write(value)


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class SetupSession:
    """Run at most 32 setup calls in one synchronous, bounded operator operation.

    Individual CLI results are provisional until the context exits successfully.
    This does not roll back completed installation steps or certify execution.
    """

    def __init__(self, *, maximum_seconds: float = 1800):
        if (
            isinstance(maximum_seconds, bool)
            or not isinstance(maximum_seconds, (int, float))
            or not 0 < maximum_seconds <= 2400
        ):
            raise DeliveryError("finite setup deadline up to 2400 seconds required")
        self.maximum_seconds = maximum_seconds
        self.completed = False
        self.records: list[dict] = []
        self._entered = False
        self._used = False
        self._failed = False
        self._owns_alarm = False

    def _owner(self) -> None:
        _require_operator()
        if (
            os.getpid() != self._pid
            or threading.get_ident() != self._thread
            or threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            raise DeliveryError("same single-threaded setup caller required")

    def _deadline(self) -> None:
        if time.monotonic() >= self._deadline_at:
            self._failed = True
            raise DeliveryError("setup operation deadline exceeded")

    def _expired(self, _signum, _frame) -> None:
        self._failed = True
        raise DeliveryError("setup operation deadline exceeded")

    def __enter__(self):
        if self._used:
            raise DeliveryError("setup session is single-use")
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._owner()
        self._used = True
        self._started = time.monotonic()
        self._deadline_at = self._started + self.maximum_seconds
        self._old_handler = signal.getsignal(signal.SIGALRM)
        self._old_timer = signal.getitimer(signal.ITIMER_REAL)
        try:
            # An earlier caller deadline retains its original handler and timer.
            if not self._old_timer[0] or self._old_timer[0] > self.maximum_seconds:
                self._owns_alarm = True
                signal.signal(signal.SIGALRM, self._expired)
                signal.setitimer(signal.ITIMER_REAL, self.maximum_seconds)
            self._scope = manifest.verification_scope()
            self._scope.__enter__()
            self._entered = True
        except BaseException:
            self._restore_alarm()
            raise
        return self

    def _restore_alarm(self) -> None:
        if self._owns_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old_handler)
            if self._old_timer[0]:
                remaining = self._old_timer[0] - (time.monotonic() - self._started)
                signal.setitimer(signal.ITIMER_REAL, max(0.000001, remaining), self._old_timer[1])
            self._owns_alarm = False

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                try:
                    self._owner()
                    self._deadline()
                    if self._failed:
                        raise DeliveryError("failed setup session cannot complete")
                except BaseException as error:
                    self._scope.__exit__(type(error), error, error.__traceback__)
                    raise
            self._scope.__exit__(kind, value, traceback)
            self.completed = kind is None and not self._failed
        finally:
            self._entered = False
            self._restore_alarm()
        return False

    def invoke(self, module: str, argv: list[str]) -> CommandResult:
        if not self._entered or self._failed:
            raise DeliveryError("active successful setup session required")
        try:
            self._owner()
            self._deadline()
            if (
                not isinstance(module, str)
                or module not in _ENTRIES
                or not isinstance(argv, list)
                or len(argv) > 64
                or any(
                    not isinstance(arg, str)
                    or not 0 < len(arg.encode("utf-8")) <= 4096
                    or "\x00" in arg
                    or arg.startswith("-h")
                    or (
                        arg.startswith("--")
                        and any(
                            private.startswith(arg.split("=", 1)[0])
                            for private in ("--help", "--inside")
                        )
                    )
                    for arg in argv
                )
                or len(self.records) >= 32
            ):
                raise DeliveryError("unknown or oversized setup CLI call")
            if module == "scripts.release" and (not argv or argv[0] not in _DELIVERY_COMMANDS):
                raise DeliveryError("command is outside offline setup")
            if module == "scripts.release.services" and (not argv or argv[0] != "generate"):
                raise DeliveryError("only service composition belongs to setup")
            if module == "scripts.release.journal" and argv:
                raise DeliveryError("fixed journal setup takes no overrides")
            runtime_option = {
                "scripts.release.dependencies": "--check-service",
                "scripts.release.proxy_install": "--check-binding",
            }.get(module)
            if runtime_option and any(
                arg.startswith("--") and runtime_option.startswith(arg.split("=", 1)[0])
                for arg in argv
            ):
                raise DeliveryError("runtime checks are outside setup")
            entry = importlib.import_module(_ENTRIES[module]).main
            stdout, stderr = _Output(32768), _Output(8192)
            started = time.monotonic()
            record = {"module": module, "actual_exit_code": None}
            self.records.append(record)
            try:
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    try:
                        entry(list(argv))
                    except SystemExit as error:
                        code = (
                            error.code
                            if type(error.code) is int
                            else (0 if error.code is None else 1)
                        )
                    else:
                        code = 0
                record["actual_exit_code"] = code
            except BaseException as error:
                record["actual_exception_type"] = type(error).__name__
                raise
            finally:
                record.update(
                    seconds=time.monotonic() - started,
                    stdout_bytes=stdout.bytes_written,
                    stdout_sha256=hashlib.sha256(stdout.getvalue().encode()).hexdigest(),
                    stderr_bytes=stderr.bytes_written,
                    stderr_sha256=hashlib.sha256(stderr.getvalue().encode()).hexdigest(),
                )
            result = CommandResult(code, stdout.getvalue(), stderr.getvalue())
            if code:
                self._failed = True
            self._deadline()
            return result
        except BaseException:
            self._failed = True
            raise
