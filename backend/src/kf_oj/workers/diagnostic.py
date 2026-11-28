"""Dispatch explicitly authorized diagnostic requests through the trusted Worker."""

import argparse
import asyncio
import fcntl
import json
import os
import re
import shutil
import signal
import stat
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import RootModel
from sqlalchemy.exc import SQLAlchemyError

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic import (
    CLAIM_SECONDS,
    MAX_CONTROL_BYTES,
    DiagnosticBinding,
    DiagnosticResult,
    DiagnosticRun,
)
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.identity.service import IdentityService
from kf_oj.modules.learning.diagnostic_inputs import DiagnosticInputsUnavailable
from kf_oj.modules.learning.diagnostic_queue import (
    BatchDiagnosticClaim,
    BatchDiagnosticRequest,
    DiagnosticClaim,
    DiagnosticExecutionInputs,
    DiagnosticExecutorSettings,
    DiagnosticQueue,
    DiagnosticRequest,
)
from kf_oj.modules.learning.diagnostic_runtime import FACTORY_STAGE_INTERVAL_US
from kf_oj.modules.problems.public import ProblemPort
from kf_oj.modules.problems.service import ProblemService
from kf_oj.workers.diagnostic_batch import preflight_registered_batch

_CHECK_INTERVAL = 0.25
_CLEANUP_GRACE = 5
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


class DiagnosticCleanupUnconfirmed(RuntimeError):
    """The broker must retain the execution slot after an uncertain Worker outcome."""


class DiagnosticPreflightFailure(ValueError):
    """A known startup refusal with a message safe to show to the operator."""


class _AdmissionFile(RootModel[v2.NodeAdmission]):
    pass


@dataclass(frozen=True, slots=True)
class DiagnosticWorker:
    binary: Path
    factory_socket: Path
    staging: Path
    admission_file: Path
    worker_id: UUID
    factory_config_sha256: str

    def __post_init__(self):
        paths = (self.binary, self.factory_socket, self.staging, self.admission_file)
        if (
            not all(path.is_absolute() for path in paths)
            or not re.fullmatch(r"[0-9a-f]{64}", self.factory_config_sha256)
            or self.worker_id.int == 0
        ):
            raise ValueError("invalid local diagnostic Worker configuration")

    @contextmanager
    def execution_lock(self) -> Iterator[tuple[int, UUID] | None]:
        """A Rust child retains the lock even if its Python broker disappears."""
        fd = os.open(self.staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.fstat(fd)
            if directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700:
                raise ValueError("diagnostic staging must be private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            machine = UUID(Path("/etc/machine-id").read_text().strip())
            if machine.int == 0:
                raise ValueError("diagnostic machine identity unavailable")
            scope = uuid5(
                machine,
                json.dumps(
                    [
                        str(self.worker_id),
                        str(self.factory_socket),
                        self.factory_config_sha256,
                        str(self.staging),
                        directory.st_dev,
                        directory.st_ino,
                    ]
                ),
            )
            yield fd, scope
        finally:
            # Closing just this descriptor leaves the child's copy locked.
            os.close(fd)

    async def confirm_cleanup(self, lock_fd: int) -> bool:
        """An authenticated root proof bound to the configuration used at launch."""
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.binary),
                "check-stopped",
                "--socket",
                str(self.factory_socket),
                "--factory-config-sha256",
                self.factory_config_sha256,
                "--diagnostic-staging",
                str(self.staging),
                env={"LC_ALL": "C.UTF-8"},
                pass_fds=(lock_fd,),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            async with asyncio.timeout(_CLEANUP_GRACE):
                reply, _ = await process.communicate()
            return process.returncode == 0 and reply == b'{"cleanup_complete":true}\n'
        except (OSError, TimeoutError):
            return False
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    def admission(self) -> v2.NodeAdmission:
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise ValueError("diagnostic Worker executable unavailable")
        fd = os.open(self.admission_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("diagnostic admission file unavailable")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError("diagnostic admission file must be private")
            contents = file.read(32769)
        if len(contents) > 32768:
            raise ValueError("diagnostic admission file is too large")
        policy = v1.decode_wire(_AdmissionFile, contents).root
        if isinstance(policy, v1.AdmissionPolicy) or (
            isinstance(policy, v2.AdmissionRegistry) and policy.multi_runtime is None
        ):
            raise ValueError("diagnostic Worker needs an exact multi-runtime admission policy")
        return policy

    async def execute(
        self,
        request: DiagnosticRun,
        bundle: DiagnosticExecutionInputs,
        check_current: Callable[[], Awaitable[bool]],
        lock_fd: int,
    ) -> DiagnosticResult:
        header = request.model_dump_json().encode()
        if len(header) > MAX_CONTROL_BYTES:
            raise DiagnosticCleanupUnconfirmed("diagnostic request is too large")
        process = await asyncio.create_subprocess_exec(
            str(self.binary),
            "diagnose-public-sample",
            "--socket",
            str(self.factory_socket),
            "--staging",
            str(self.staging),
            "--admission",
            str(self.admission_file),
            "--worker-id",
            str(self.worker_id),
            "--factory-config-sha256",
            self.factory_config_sha256,
            env={"LC_ALL": "C.UTF-8"},
            pass_fds=(lock_fd,),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        writer, reader = process.stdin, process.stdout
        assert writer is not None and reader is not None

        async def read_result() -> DiagnosticResult:
            data = bytearray()
            while part := await reader.read(min(4096, MAX_CONTROL_BYTES + 1 - len(data))):
                data.extend(part)
                if len(data) > MAX_CONTROL_BYTES:
                    raise DiagnosticCleanupUnconfirmed("diagnostic Worker reply is too large")
            if await process.wait() != 0:
                raise DiagnosticCleanupUnconfirmed("diagnostic Worker did not acknowledge cleanup")
            try:
                result = v1.decode_wire(DiagnosticResult, bytes(data))
            except ValueError:
                raise DiagnosticCleanupUnconfirmed("diagnostic Worker reply unavailable") from None
            if (
                result.binding != request.binding
                or result.state == "cleanup_unconfirmed"
                or result.state == "observed"
                and (result.ubsan is not None) != (request.mode == "ubsan")
            ):
                raise DiagnosticCleanupUnconfirmed("diagnostic cleanup was not confirmed")
            return result

        response = asyncio.create_task(read_result())
        try:
            async with asyncio.timeout(CLAIM_SECONDS + _CLEANUP_GRACE):
                for chunk in (
                    len(header).to_bytes(4, "big"),
                    header,
                    bundle.source,
                    bundle.inputs.sample.input_bytes,
                    bundle.inputs.sample.expected_bytes,
                ):
                    writer.write(chunk)
                await writer.drain()
                while not response.done():
                    await asyncio.wait((response,), timeout=_CHECK_INTERVAL)
                    if not response.done() and not await check_current():
                        writer.close()
                        break
                return await response
        except (OSError, TimeoutError, SQLAlchemyError):
            raise DiagnosticCleanupUnconfirmed("diagnostic Worker was interrupted") from None
        finally:
            writer.close()
            if not response.done():
                response.cancel()
            await asyncio.gather(response, return_exceptions=True)
            if process.returncode is None:
                try:
                    async with asyncio.timeout(_CLEANUP_GRACE):
                        await process.wait()
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()


def _request(
    claim: DiagnosticClaim, bundle: DiagnosticExecutionInputs, worker_id: UUID
) -> DiagnosticRun:
    return DiagnosticRun(
        schema_version=1,
        kind="diagnostic.run",
        mode=claim.mode,
        binding=DiagnosticBinding(
            request_id=claim.request_id, execution_id=claim.execution_id, worker_id=worker_id
        ),
        problem_version_id=bundle.inputs.source.problem_version_id,
        configuration=bundle.configuration,
        claim_expires_at_unix_ms=(claim.execution_deadline - _EPOCH) // _MILLISECOND,
        source=bundle.inputs.source.artifact,
        sample=v1.TestCase(
            case_id=claim.execution_id,
            input=ArtifactStore.reference(bundle.inputs.sample.input_bytes),
            expected=ArtifactStore.reference(bundle.inputs.sample.expected_bytes),
        ),
    )


def _clear_batch_inputs(directory: Path) -> bool:
    """Clear only the private payload associated with the current broker scope."""
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True


async def _dispatch_batch(
    queue: DiagnosticQueue,
    claim: BatchDiagnosticClaim,
    artifacts: ArtifactRepository,
    worker: DiagnosticWorker,
    lock_fd: int,
    directory: Path,
    executor: DiagnosticExecutorSettings,
) -> BatchDiagnosticRequest:
    # Standalone replay also uses DiagnosticWorker from this module.
    from kf_oj.workers.diagnostic_batch_replay import (
        DiagnosticReplayStopped,
        batch_summary,
        replay_admitted_batch,
    )

    if not await worker.confirm_cleanup(lock_fd) or directory.exists() or directory.is_symlink():
        raise DiagnosticCleanupUnconfirmed("prior batch cleanup was not confirmed")

    try:
        bundle = await queue.batch_execution_inputs(claim, artifacts, executor=executor)
    except DiagnosticInputsUnavailable:
        return await queue.acknowledge_batch_stopped(
            claim, failure_code=v1.FailureCode.ARTIFACT_UNAVAILABLE
        )
    batch = bundle.batch
    if batch.receipt()["state"] == "reference_pending":
        return await queue.acknowledge_batch_stopped(
            claim, summary=batch_summary(batch, claim.source, batch.receipt())
        )

    async def check_current() -> bool:
        return await queue.claim_current(claim)

    if not await check_current():
        return await queue.acknowledge_batch_stopped(
            claim, failure_code=v1.FailureCode.WORKER_SHUTDOWN
        )
    directory.mkdir(mode=0o700)
    remaining = (claim.execution_deadline - datetime.now(UTC)).total_seconds()
    deadline = asyncio.get_running_loop().time() + remaining
    summary = None
    failure = None
    try:
        result = await replay_admitted_batch(
            batch,
            bundle.source,
            worker,
            directory,
            lock_fd,
            claim_deadline=deadline,
            check_current=check_current,
        )
        summary = batch_summary(batch, claim.source, result)
    except DiagnosticReplayStopped:
        failure = v1.FailureCode.WORKER_SHUTDOWN
    except TimeoutError:
        failure = v1.FailureCode.JOB_DEADLINE
    except (ValueError, OSError):
        failure = v1.FailureCode.ARTIFACT_UNAVAILABLE
    if not await worker.confirm_cleanup(lock_fd) or not _clear_batch_inputs(directory):
        raise DiagnosticCleanupUnconfirmed("batch cleanup was not confirmed")
    return await queue.acknowledge_batch_stopped(claim, summary=summary, failure_code=failure)


async def dispatch_once(
    queue: DiagnosticQueue,
    artifacts: ArtifactRepository,
    problems: ProblemPort,
    worker: DiagnosticWorker,
) -> DiagnosticRequest | BatchDiagnosticRequest | None:
    with worker.execution_lock() as lease:
        if lease is None:
            return None
        lock_fd, scope = lease
        batch_directory = worker.staging.parent / f".kf-diagnostic-batch-{scope.hex}"

        async def cleanup() -> bool:
            return await worker.confirm_cleanup(lock_fd) and _clear_batch_inputs(batch_directory)

        recovered = await queue.recover_expired(scope, cleanup)
        if recovered is not None:
            return recovered

        admission = worker.admission()
        executor = DiagnosticExecutorSettings(
            factory_config_sha256=worker.factory_config_sha256,
            admission_sha256=sha256(admission.model_dump_json().encode()).hexdigest(),
        )
        claim = await queue.claim(scope)
        if claim is None:
            return None
        if isinstance(claim, BatchDiagnosticClaim):
            return await _dispatch_batch(
                queue, claim, artifacts, worker, lock_fd, batch_directory, executor
            )
        try:
            bundle = await queue.execution_inputs(
                claim, artifacts, problems, admission, executor=executor
            )
            request = _request(claim, bundle, worker.worker_id)
        except ValueError:
            return await queue.acknowledge_stopped(
                claim, failure_code=v1.FailureCode.ARTIFACT_UNAVAILABLE
            )

        if not await queue.claim_current(claim):
            return await queue.acknowledge_stopped(
                claim, failure_code=v1.FailureCode.WORKER_SHUTDOWN
            )
        result = await worker.execute(request, bundle, lambda: queue.claim_current(claim), lock_fd)
        failure = (
            v1.FailureCode.WORKER_SHUTDOWN if result.state == "cancelled" else result.failure_code
        )
        return await queue.acknowledge_stopped(
            claim, observation=result.ubsan or result.observation, failure_code=failure
        )


async def _serve(
    dispatch: Callable[[], Awaitable[DiagnosticRequest | BatchDiagnosticRequest | None]],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        result = await dispatch()
        if result is not None:
            print(result.state, flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=FACTORY_STAGE_INTERVAL_US / 1_000_000)
        except TimeoutError:
            continue


async def _main(
    settings: Settings,
    worker: DiagnosticWorker,
    batch_registry: Path | None = None,
    *,
    mode: Literal["once", "check", "serve"] = "once",
) -> None:
    if os.geteuid() < 1000 or os.getegid() < 1000 or settings.artifact_root is None:
        raise DiagnosticPreflightFailure(
            "an ordinary account and private artifact storage are required"
        )
    if mode != "once" and not settings.learning_diagnostics_enabled:
        raise DiagnosticPreflightFailure("diagnostics are disabled")
    if mode != "once" and settings.learning_diagnostic_batch_offers and batch_registry is None:
        raise DiagnosticPreflightFailure("advertised batches require a local registry")
    try:
        store = ArtifactStore(
            settings.artifact_root,
            max_bytes=settings.artifact_max_bytes,
            max_files=settings.artifact_max_files,
        )
    except (OSError, ValueError):
        raise DiagnosticPreflightFailure(
            "private diagnostic artifact storage unavailable"
        ) from None
    engine = None
    try:
        try:
            engine = build_engine(settings)
        except (ValueError, SQLAlchemyError):
            raise DiagnosticPreflightFailure(
                "diagnostic queue database configuration unavailable"
            ) from None
        sessions = build_session_factory(engine)
        artifacts = ArtifactRepository(store)
        problems = ProblemService(sessions, artifacts, settings.judge_admission)

        async def load_batch(db, digest):
            return await preflight_registered_batch(
                db, problems, artifacts, worker.admission(), batch_registry, digest
            )

        queue = DiagnosticQueue(
            sessions,
            IdentityService(sessions),
            ubsan_enabled=settings.learning_diagnostics_ubsan_enabled,
            batch_offers=(
                settings.learning_diagnostic_batch_offers
                if mode != "once" or settings.learning_diagnostic_batch_offers
                else None
            ),
            load_batch=load_batch if batch_registry is not None else None,
        )
        if mode == "once":
            result = await dispatch_once(queue, artifacts, problems, worker)
            print("idle" if result is None else result.state)
            return

        try:
            admission = worker.admission()
        except (OSError, ValueError):
            raise DiagnosticPreflightFailure(
                "Worker binary or admission configuration unavailable"
            ) from None
        try:
            with worker.execution_lock() as lease:
                ready = lease is not None and await worker.confirm_cleanup(lease[0])
        except (OSError, ValueError):
            raise DiagnosticPreflightFailure(
                "diagnostic staging directory or machine identity unavailable"
            ) from None
        if not ready:
            raise DiagnosticPreflightFailure("diagnostic factory readiness is unconfirmed")
        async with sessions.begin() as db:
            try:
                permitted = await queue.executor_has_access(db)
            except (OSError, SQLAlchemyError):
                raise DiagnosticPreflightFailure(
                    "diagnostic queue database or read permissions unavailable"
                ) from None
            if not permitted:
                raise DiagnosticPreflightFailure(
                    "diagnostic queue role cannot claim tasks and publish results"
                )
            for offer in settings.learning_diagnostic_batch_offers:
                try:
                    batch = await preflight_registered_batch(
                        db, problems, artifacts, admission, batch_registry, offer.manifest_sha256
                    )
                except (OSError, ValueError, SQLAlchemyError):
                    raise DiagnosticPreflightFailure(
                        "registered diagnostic batch failed validation"
                    ) from None
                if (
                    batch.manifest.problem_version_id != offer.problem_version_id
                    or batch.manifest.language != offer.language
                    or batch.manifest.purpose != offer.purpose
                ):
                    raise DiagnosticPreflightFailure(
                        "advertised diagnostic batch differs from its registry"
                    )
        if mode == "check":
            print(
                json.dumps(
                    {"service": "diagnostic-broker", "status": "ready", "consuming_tasks": False}
                )
            )
            return

        async def dispatch() -> DiagnosticRequest | BatchDiagnosticRequest | None:
            return await dispatch_once(queue, artifacts, problems, worker)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signo in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signo, stop.set)
        try:
            await _serve(dispatch, stop)
        finally:
            for signo in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signo)
    finally:
        if engine is not None:
            await engine.dispose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--factory-socket", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--admission-file", type=Path, required=True)
    parser.add_argument("--worker-id", type=UUID, required=True)
    parser.add_argument("--factory-config-sha256", required=True)
    parser.add_argument("--batch-registry", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_const",
        const="check",
        dest="mode",
        help="Verify the factory and registered batches without consuming tasks",
    )
    mode.add_argument(
        "--serve",
        action="store_const",
        const="serve",
        dest="mode",
        help="Continuously process authorized requests until stopped",
    )
    options = vars(parser.parse_args())
    registry = options.pop("batch_registry")
    selected = options.pop("mode") or "once"
    try:
        asyncio.run(_main(Settings(), DiagnosticWorker(**options), registry, mode=selected))
    except DiagnosticPreflightFailure as error:
        raise SystemExit(f"diagnostic startup: {error}") from None
    except DiagnosticCleanupUnconfirmed:
        raise SystemExit("diagnostic cleanup is unconfirmed; execution slot retained") from None
    except (OSError, ValueError, SQLAlchemyError):
        raise SystemExit("diagnostic broker unavailable") from None


if __name__ == "__main__":
    main()
