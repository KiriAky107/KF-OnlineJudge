"""Run an operator-pinned batch through the isolated GNU Worker."""

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic import MAX_CONTROL_BYTES
from kf_oj.contracts.diagnostic_batch import (
    BatchDiagnosticSummary,
    ConfirmBatchDiagnostic,
    DiagnosticBoundaryReport,
)
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore
from kf_oj.modules.identity.public import ActiveSessionPort, Principal
from kf_oj.modules.learning.diagnostic_batch import DiagnosticBatch
from kf_oj.modules.learning.diagnostic_inputs import (
    DiagnosticInputsUnavailable,
    resolve_confirmed_diagnostic_source,
)
from kf_oj.modules.learning.diagnostic_runtime import FACTORY_STAGE_INTERVAL_US
from kf_oj.modules.problems.local_files import LocalImportFiles
from kf_oj.modules.problems.public import ProblemPort, ProblemUnavailable
from kf_oj.modules.submissions.public import DiagnosticSourceReference
from kf_oj.workers.diagnostic import DiagnosticCleanupUnconfirmed, DiagnosticWorker
from kf_oj.workers.diagnostic_batch import load_operator_batch

_CLEANUP_GRACE = 5
_CHECK_INTERVAL = 0.25
_RESOURCE_LIMITS = frozenset(
    (v1.Verdict.TIME_LIMIT, v1.Verdict.MEMORY_LIMIT, v1.Verdict.OUTPUT_LIMIT)
)


class DiagnosticReplayStopped(RuntimeError):
    """The registered batch claim is no longer live."""


async def _worker_report[Report: v1.WireModel](
    worker: DiagnosticWorker,
    job: Path,
    artifacts: Path,
    deadline: float,
    lock_fd: int,
    report_type: type[Report],
    *,
    check_current: Callable[[], Awaitable[bool]] | None = None,
) -> Report:
    mode = ("--measure-resources",) if report_type is DiagnosticBoundaryReport else ()
    try:
        process = await asyncio.create_subprocess_exec(
            str(worker.binary),
            "replay-diagnostic-batch",
            "--socket",
            str(worker.factory_socket),
            "--job",
            str(job),
            "--artifacts",
            str(artifacts),
            "--staging",
            str(worker.staging),
            "--admission",
            str(worker.admission_file),
            "--worker-id",
            str(worker.worker_id),
            "--factory-config-sha256",
            worker.factory_config_sha256,
            *mode,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"LC_ALL": "C.UTF-8"},
            pass_fds=(lock_fd,),
        )
        assert process.stdin is not None and process.stdout is not None

        async def read_report() -> Report:
            data = bytearray()
            while chunk := await process.stdout.read(min(4096, MAX_CONTROL_BYTES + 1 - len(data))):
                data.extend(chunk)
                if len(data) > MAX_CONTROL_BYTES:
                    raise ValueError("diagnostic report exceeded bounds")
            if await process.wait() != 0:
                raise ValueError("diagnostic replay worker refused")
            return v1.decode_wire(report_type, bytes(data))

        response = asyncio.create_task(read_report())
        try:
            async with asyncio.timeout_at(deadline):
                if check_current is not None:
                    while not response.done():
                        await asyncio.wait((response,), timeout=_CHECK_INTERVAL)
                        if not response.done() and not await check_current():
                            raise DiagnosticReplayStopped()
                return await response
        finally:
            process.stdin.close()
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
    finally:
        if not await worker.confirm_cleanup(lock_fd):
            raise DiagnosticCleanupUnconfirmed("diagnostic replay cleanup was not confirmed")


async def _replay_source[Report: v1.WireModel](
    batch: DiagnosticBatch,
    worker: DiagnosticWorker,
    source: bytes,
    cases: tuple[v1.TestCase, ...],
    directory: Path,
    deadline: float,
    lock_fd: int,
    report_type: type[Report],
    *,
    check_current: Callable[[], Awaitable[bool]] | None = None,
) -> Report:
    if check_current is not None and not await check_current():
        raise DiagnosticReplayStopped()
    config = batch.configuration
    remaining_us = int((deadline - asyncio.get_running_loop().time()) * 1_000_000)
    needed_us = config.compile_limits.wall_time_us + len(cases) * config.run_limits.wall_time_us
    if remaining_us <= 0:
        raise TimeoutError("diagnostic replay deadline is exhausted")
    ref = ArtifactStore.reference(source)
    (directory / "artifacts" / ref.digest).write_bytes(source)
    binding = v1.LeaseBinding(
        evaluation_id=uuid4(), attempt_id=uuid4(), worker_id=worker.worker_id, fencing_version=1
    )
    manifest = v2.JobManifest(
        schema_version=2,
        kind="judge.manifest",
        evaluation_id=binding.evaluation_id,
        submission_id=uuid4(),
        problem_version_id=batch.manifest.problem_version_id,
        source=ref,
        cases=cases,
        self_test=False,
        comparison="exact_bytes",
        **config.model_dump(exclude={"job_wall_time_us"}),
        # The manifest reserves the full stage ceilings. The broker also
        # enforces the shared deadline, including its own staging overhead.
        job_wall_time_us=max(needed_us, remaining_us),
    )
    digest = sha256(v1.manifest_bytes(manifest)).hexdigest()
    job = directory / "job.json"
    job.write_text(
        json.dumps(
            {
                "manifest": manifest.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
                "manifest_digest": digest,
                "attempt_number": 1,
            },
            separators=(",", ":"),
        )
    )
    report = await _worker_report(
        worker,
        job,
        directory / "artifacts",
        deadline,
        lock_fd,
        report_type,
        check_current=check_current,
    )
    if report.binding != binding or report.manifest_digest != digest:
        raise ValueError("diagnostic replay report unavailable")
    if isinstance(report, DiagnosticBoundaryReport):
        if tuple(case.case_id for case in report.cases) != tuple(case.case_id for case in cases):
            raise ValueError("diagnostic boundary report differs from admitted cases")
    else:
        report.validate_for_manifest(manifest, digest)
        if (
            not report.cleanup_complete
            or report.failure_code is not None
            or report.verdict in (v1.Verdict.CANCELLED, v1.Verdict.SYSTEM_ERROR)
            or report.compilation.diagnostics
            or report.compilation.diagnostics_truncated
        ):
            raise ValueError("diagnostic replay report unavailable")
    return report


def _candidate_category(report: v2.JudgementReport) -> str:
    outcomes = {case.verdict for case in report.cases}
    if report.verdict == v1.Verdict.COMPILATION_ERROR:
        return "compilation_error"
    if outcomes & _RESOURCE_LIMITS:
        return "resource_limit"
    if v1.Verdict.RUNTIME_ERROR in outcomes:
        return "runtime_error"
    if v1.Verdict.WRONG_ANSWER in outcomes:
        return "wrong_answer"
    if report.verdict == v1.Verdict.ACCEPTED:
        return "accepted"
    raise ValueError("diagnostic replay did not observe the candidate")


def _smaller_failure(
    cases: tuple[v1.TestCase, ...], report: v2.JudgementReport
) -> v1.TestCase | None:
    wrong = {item.case_id for item in report.cases if item.verdict == v1.Verdict.WRONG_ANSWER}
    if cases[0].case_id not in wrong:
        return None
    return min(
        (item for item in cases[1:] if item.case_id in wrong),
        key=lambda item: (item.input.bytes, item.input.digest),
        default=None,
    )


def _repeat_pair(pair: tuple[v1.TestCase, ...]) -> tuple[v1.TestCase, ...]:
    return pair + tuple(case.model_copy(update={"case_id": uuid4()}) for case in pair)


async def replay_batch(batch: DiagnosticBatch, candidate: bytes, worker: DiagnosticWorker) -> dict:
    """Run an operator's local replay under the diagnostic staging lock."""
    if batch.receipt()["state"] == "reference_pending":
        return batch.receipt()
    with worker.execution_lock() as lease:
        if lease is None:
            raise ValueError("diagnostic slot unavailable")
        lock_fd, _ = lease
        if not await worker.confirm_cleanup(lock_fd):
            raise DiagnosticCleanupUnconfirmed("prior diagnostic cleanup was not confirmed")
        with TemporaryDirectory(prefix="kf-diagnostic-batch-", dir=worker.staging.parent) as work:
            return await replay_admitted_batch(batch, candidate, worker, Path(work), lock_fd)


async def replay_admitted_batch(
    batch: DiagnosticBatch,
    candidate: bytes,
    worker: DiagnosticWorker,
    directory: Path,
    lock_fd: int,
    *,
    claim_deadline: float | None = None,
    check_current: Callable[[], Awaitable[bool]] | None = None,
) -> dict:
    """Use the caller's existing staging lock, and stay within its claim deadline."""
    receipt = batch.receipt()
    if receipt["state"] == "reference_pending":
        return receipt
    boundary = batch.manifest.purpose == "boundary"
    reduction = batch.manifest.reduction is not None
    search = batch.manifest.searches_reduction
    if not boundary and batch.reference_source is None:
        raise ValueError("differential replay requires a reviewed reference")
    if not 1 <= len(candidate) <= v1.MAX_SOURCE_BYTES:
        raise ValueError("diagnostic candidate size outside policy")
    checker = batch.input_check
    if reduction and checker is None:
        raise ValueError("reduction requires an admitted input checker")
    if checker is not None and candidate == checker.source:
        raise ValueError("input checker must be independent of the candidate")
    artifacts = directory / "artifacts"
    artifacts.mkdir(mode=0o700)
    cases = []
    for data, expected in batch.cases:
        if expected is None and not boundary:
            raise ValueError("independent expected output unavailable")
        stdin = ArtifactStore.reference(data)
        answer = ArtifactStore.reference(expected if expected is not None else b"")
        (artifacts / stdin.digest).write_bytes(data)
        if expected is not None:
            (artifacts / answer.digest).write_bytes(expected)
        cases.append(v1.TestCase(case_id=uuid4(), input=stdin, expected=answer))
    sequence = tuple(cases)
    config = batch.configuration
    clock = asyncio.get_running_loop().time
    deadline = clock() + config.job_wall_time_us / 1_000_000
    if claim_deadline is not None:
        deadline = min(deadline, claim_deadline)
    stage_interval = FACTORY_STAGE_INTERVAL_US / 1_000_000
    next_stage_at = clock()

    async def stage[Report: v1.WireModel](
        admitted: DiagnosticBatch,
        source: bytes,
        inputs: tuple[v1.TestCase, ...],
        until: float,
        report_type: type[Report],
    ) -> Report:
        nonlocal next_stage_at
        if next_stage_at >= until:
            raise TimeoutError("diagnostic replay deadline is exhausted")
        remaining_gap = next_stage_at - clock()
        if remaining_gap > 0:
            await asyncio.sleep(remaining_gap)
        result = await _replay_source(
            admitted,
            worker,
            source,
            inputs,
            directory,
            until,
            lock_fd,
            report_type,
            check_current=check_current,
        )
        next_stage_at = clock() + stage_interval
        return result

    pass_reserve = (
        config.compile_limits.wall_time_us + len(sequence) * config.run_limits.wall_time_us
    ) / 1_000_000
    verification_reserve = (
        (config.compile_limits.wall_time_us + 4 * config.run_limits.wall_time_us) / 1_000_000
        + stage_interval
        if search
        else 0
    )
    candidate_reserve = pass_reserve + stage_interval + verification_reserve
    if reduction and not search:
        candidate_reserve += len(sequence) * config.run_limits.wall_time_us / 1_000_000
    if checker is not None:
        marker = ArtifactStore.reference(b"")
        input_cases = tuple(case.model_copy(update={"expected": marker}) for case in sequence)
        checker_deadline = min(
            deadline
            - (
                candidate_reserve if boundary else pass_reserve + stage_interval + candidate_reserve
            ),
            clock() + checker.configuration.job_wall_time_us / 1_000_000,
        )
        verified = await stage(
            replace(batch, configuration=checker.configuration),
            checker.source,
            input_cases,
            checker_deadline,
            DiagnosticBoundaryReport,
        )
        if any(case.outcome != "completed" for case in verified.cases):
            return receipt | {"state": "input_not_verified"}
        receipt["inputs_verified"] = True
    if boundary:
        result = await stage(
            batch,
            candidate,
            sequence,
            deadline,
            DiagnosticBoundaryReport,
        )
        system = os.uname()
        return receipt | {
            "state": "boundary_observed",
            "sampling": {
                "kernel": system.release,
                "architecture": system.machine,
                "toolchain": config.toolchain.model_dump(mode="json"),
                "run_limits": config.run_limits.model_dump(mode="json"),
                "factory_config_sha256": worker.factory_config_sha256,
            },
            "measurements": [
                case.model_dump(mode="json", exclude={"case_id"}) for case in result.cases
            ],
        }
    reference = await stage(
        batch,
        batch.reference_source,
        sequence,
        deadline - candidate_reserve,
        v2.JudgementReport,
    )
    if reference.verdict != v1.Verdict.ACCEPTED:
        return receipt | {"state": "reference_pending"}
    candidate_cases = sequence
    if reduction and not search:
        candidate_cases = _repeat_pair(sequence)
    result = await stage(
        batch,
        candidate,
        candidate_cases,
        deadline - verification_reserve,
        v2.JudgementReport,
    )
    selected = None
    if search:
        selected = _smaller_failure(sequence, result)
        if selected is None:
            return receipt | {"state": "reduction_unverified"}
        candidate_cases = _repeat_pair((sequence[0], selected))
        result = await stage(
            batch,
            candidate,
            candidate_cases,
            deadline,
            v2.JudgementReport,
        )
    if reduction:
        reproduced = len(result.cases) == len(candidate_cases) and all(
            case.verdict == v1.Verdict.WRONG_ANSWER for case in result.cases
        )
        outcome = receipt | {
            "state": "reduction_verified" if reproduced else "reduction_unverified",
        }
        if reproduced and selected is not None:
            outcome["selected_input_sha256"] = selected.input.digest
        return outcome
    return receipt | {
        "state": "candidate_observed",
        "category": _candidate_category(result),
    }


async def replay_confirmed_batch(
    sessions: async_sessionmaker,
    identity: ActiveSessionPort,
    problems: ProblemPort,
    artifacts: ArtifactRepository,
    principal: Principal,
    request: ConfirmBatchDiagnostic,
    batch: DiagnosticBatch,
    worker: DiagnosticWorker,
) -> BatchDiagnosticSummary:
    """Run with an owner's current consent and publish only a scoped outcome."""
    if (
        request.problem_version_id != batch.manifest.problem_version_id
        or request.manifest_sha256 != batch.manifest_sha256
    ):
        raise DiagnosticInputsUnavailable("diagnostic input unavailable")

    async def source_for(session):
        source = await resolve_confirmed_diagnostic_source(
            session,
            identity,
            principal,
            submission_id=request.submission_id,
            problem_version_id=request.problem_version_id,
            source_execution_confirmed=request.source_execution_confirmed,
        )
        if source.language != batch.manifest.language:
            raise DiagnosticInputsUnavailable("diagnostic input unavailable")
        try:
            published = await problems.for_self_test(session, request.problem_version_id)
        except ProblemUnavailable:
            raise DiagnosticInputsUnavailable("diagnostic input unavailable") from None
        if source.language not in published.languages:
            raise DiagnosticInputsUnavailable("diagnostic input unavailable")
        return source

    async with sessions.begin() as session:
        initial = await source_for(session)
        try:
            candidate = (
                b""
                if batch.receipt()["state"] == "reference_pending"
                else await artifacts.read(session, initial.artifact)
            )
        except ArtifactError:
            raise DiagnosticInputsUnavailable("diagnostic input unavailable") from None

    private = await replay_batch(batch, candidate, worker)
    async with sessions.begin() as session:
        if await source_for(session) != initial:
            raise DiagnosticInputsUnavailable("diagnostic input unavailable")

    return batch_summary(batch, initial, private)


def batch_summary(
    batch: DiagnosticBatch, source: DiagnosticSourceReference, private: dict
) -> BatchDiagnosticSummary:
    """Project the restricted owner-side receipt from trusted stage outcomes."""
    match private["state"]:
        case "candidate_observed":
            outcome = private["category"]
        case "boundary_observed":
            observed = {sample["outcome"] for sample in private["measurements"]}
            if observed & {"time_limit", "memory_limit", "output_limit", "other_limit"}:
                outcome = "resource_limit"
            elif "runtime_error" in observed:
                outcome = "runtime_error"
            elif observed == {"completed"}:
                outcome = "completed"
            else:
                raise DiagnosticInputsUnavailable("diagnostic result unavailable")
        case (
            "reference_pending"
            | "input_not_verified"
            | "reduction_verified"
            | "reduction_unverified"
        ):
            outcome = private["state"]
        case _:
            raise DiagnosticInputsUnavailable("diagnostic result unavailable")
    return BatchDiagnosticSummary(
        schema_version=1,
        submission_id=source.submission_id,
        problem_version_id=source.problem_version_id,
        purpose=batch.manifest.purpose,
        outcome=outcome,
    )


async def run(
    directory: Path,
    manifest_sha256: str,
    candidate_file: str,
    candidate_sha256: str,
    worker: DiagnosticWorker,
) -> dict:
    if os.geteuid() < 1000 or os.getegid() < 1000:
        raise ValueError("an ordinary diagnostic broker is required")
    files = LocalImportFiles(directory)
    try:
        batch = await load_operator_batch(files, manifest_sha256, Settings(), worker.admission())
        if batch.receipt()["state"] == "reference_pending":
            return batch.receipt()
        source = files.read(candidate_file, v1.MAX_SOURCE_BYTES)
        if sha256(source).hexdigest() != candidate_sha256:
            raise ValueError("candidate source differs from the operator pin")
    finally:
        files.close()
    return await replay_batch(batch, source, worker)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--candidate-file", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--factory-socket", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--admission-file", type=Path, required=True)
    parser.add_argument("--worker-id", type=UUID, required=True)
    parser.add_argument("--factory-config-sha256", required=True)
    args = vars(parser.parse_args())
    params = [
        args.pop(key)
        for key in ("directory", "manifest_sha256", "candidate_file", "candidate_sha256")
    ]
    try:
        receipt = asyncio.run(run(*params, DiagnosticWorker(**args)))
    except DiagnosticCleanupUnconfirmed:
        parser.exit(2, "Diagnostic replay cleanup is unconfirmed. The staging remains fenced.\n")
    except (OSError, ValueError, SQLAlchemyError):
        parser.exit(1, "Diagnostic replay unavailable.\n")
    print(json.dumps(receipt, separators=(",", ":")))


if __name__ == "__main__":
    main()
