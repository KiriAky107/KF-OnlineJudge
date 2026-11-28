"""Verify the broker's ordering separately from the isolated Worker acceptance."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic_batch import (
    BoundaryMeasurement,
    DiagnosticBatchManifest,
    DiagnosticBoundaryReport,
)
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.modules.learning.diagnostic_batch import DiagnosticBatch, DiagnosticInputCheck
from kf_oj.modules.learning.diagnostic_runtime import (
    FACTORY_STAGE_INTERVAL_US,
    diagnostic_checker_configuration,
)
from kf_oj.modules.submissions.public import DiagnosticSourceReference
from kf_oj.workers import diagnostic_batch_replay as replay
from kf_oj.workers.diagnostic import DiagnosticCleanupUnconfirmed

_REFERENCE = b"reviewed reference bytes"
_CANDIDATE = b"candidate bytes"
_CASES = ((b"1 2\n", b"3\n"), (b"4 6\n", b"10\n"))


def batch(*, model_candidate=False, boundary=False):
    root = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
    fixture = v2.JobManifest.model_validate_json(
        (root / "judge-manifest.valid-c17.json").read_bytes()
    )
    configuration = v2.LanguageConfiguration(
        language=fixture.language,
        toolchain=fixture.toolchain,
        compile_limits=fixture.compile_limits,
        run_limits=fixture.run_limits,
        job_wall_time_us=fixture.job_wall_time_us,
    )

    def file(name, data):
        return {"name": name, "artifact": ArtifactStore.reference(data)}

    reference = None
    oracle = None
    cases = tuple((data, None) for data, _ in _CASES) if model_candidate or boundary else _CASES
    if model_candidate:
        reference = {"origin": "model_candidate", "source": ArtifactStore.reference(_REFERENCE)}
    elif not boundary:
        reference = {
            "origin": "operator_reviewed",
            "source": ArtifactStore.reference(_REFERENCE),
            "submission_id": uuid4(),
            "evaluation_id": uuid4(),
            "accepted_report_sha256": "a" * 64,
        }
        oracle = {"method": "arithmetic", "revision": file("oracle", b"independent arithmetic")}
    manifest = DiagnosticBatchManifest(
        schema_version=1,
        kind="diagnostic.batch-inputs",
        problem_version_id=uuid4(),
        language=fixture.language,
        purpose="boundary" if boundary else "differential",
        generation={"recipe": "pairs", "revision": file("generator", b"fixed pairs"), "seed": 1},
        oracle=oracle,
        reference=reference,
        cases=tuple(
            {
                "input": file(f"input-{index}", data),
                "independent_expected": file(f"answer-{index}", answer) if answer else None,
            }
            for index, (data, answer) in enumerate(cases)
        ),
    )
    return DiagnosticBatch(
        manifest,
        configuration,
        "b" * 64,
        cases,
        None if model_candidate or boundary else _REFERENCE,
    )


class Broker:
    factory_config_sha256 = "c" * 64

    def __init__(self, tmp_path, *, clean):
        self.staging = tmp_path / "stage"
        self.clean = clean

    @contextmanager
    def execution_lock(self):
        yield 42, uuid4()

    async def confirm_cleanup(self, lock_fd):
        assert lock_fd == 42
        return self.clean


@pytest.mark.parametrize(
    ("reference_verdict", "state", "sources"),
    [
        (v1.Verdict.ACCEPTED, "candidate_observed", [_REFERENCE, _CANDIDATE]),
        (v1.Verdict.WRONG_ANSWER, "reference_pending", [_REFERENCE]),
    ],
)
def test_candidate_runs_only_after_the_reference(
    monkeypatch, tmp_path, reference_verdict, state, sources
):
    calls = []

    async def run_source(
        _batch, _worker, source, cases, directory, deadline, _lock_fd, report_type, *, check_current
    ):
        assert report_type is v2.JudgementReport
        calls.append((source, deadline))
        assert len(cases) == len(_CASES)
        for case, (data, expected) in zip(cases, _CASES, strict=True):
            assert (directory / "artifacts" / case.input.digest).read_bytes() == data
            assert (directory / "artifacts" / case.expected.digest).read_bytes() == expected
        verdict = reference_verdict if source == _REFERENCE else v1.Verdict.WRONG_ANSWER
        return SimpleNamespace(verdict=verdict, cases=[SimpleNamespace(verdict=verdict)])

    monkeypatch.setattr(replay, "_replay_source", run_source)
    result = asyncio.run(replay.replay_batch(batch(), _CANDIDATE, Broker(tmp_path, clean=True)))
    assert [source for source, _ in calls] == sources
    assert result["state"] == state
    if len(calls) == 2:
        assert calls[0][1] < calls[1][1]
        assert result["category"] == "wrong_answer"
    else:
        assert "category" not in result
    assert not list(tmp_path.iterdir())


def test_pending_model_reference_does_not_start_a_worker():
    result = asyncio.run(replay.replay_batch(batch(model_candidate=True), _CANDIDATE, None))
    assert result["state"] == "reference_pending"


def test_replay_refuses_if_prior_cleanup_cannot_be_confirmed(tmp_path):
    with pytest.raises(DiagnosticCleanupUnconfirmed):
        asyncio.run(replay.replay_batch(batch(), _CANDIDATE, Broker(tmp_path, clean=False)))
    assert not list(tmp_path.iterdir())


def test_running_batch_closes_worker_control_on_lost_claim(monkeypatch, tmp_path):
    async def exercise():
        closed = asyncio.Event()
        stream = asyncio.StreamReader()
        process = SimpleNamespace(
            stdin=SimpleNamespace(close=closed.set), stdout=stream, returncode=None
        )

        async def wait():
            await closed.wait()
            process.returncode = 0
            return 0

        process.wait = wait

        async def spawn(*args, **kwargs):
            assert kwargs["pass_fds"] == (42,)
            return process

        cleaned = []

        async def cleanup(lock_fd):
            assert lock_fd == 42 and process.returncode == 0
            cleaned.append(True)
            return True

        async def revoked():
            return False

        worker = SimpleNamespace(
            binary=tmp_path / "worker",
            factory_socket=tmp_path / "factory",
            staging=tmp_path / "stage",
            admission_file=tmp_path / "admission",
            worker_id=uuid4(),
            factory_config_sha256="a" * 64,
            confirm_cleanup=cleanup,
        )
        monkeypatch.setattr(replay.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(replay, "_CHECK_INTERVAL", 0.001)
        with pytest.raises(replay.DiagnosticReplayStopped):
            await replay._worker_report(
                worker,
                tmp_path / "job",
                tmp_path / "artifacts",
                asyncio.get_running_loop().time() + 1,
                42,
                v2.JudgementReport,
                check_current=revoked,
            )
        assert closed.is_set() and cleaned == [True]

    asyncio.run(exercise())


@pytest.mark.parametrize("stable", [True, False])
def test_reduction_search_ranks_only_screened_failures_and_retests_the_pair(
    monkeypatch, tmp_path, stable
):
    fixture = batch()
    generated = (
        (b"original-data\n", b"1\n"),
        (b"bc\n", b"2\n"),
        (b"cb\n", b"3\n"),
        (b"a\n", b"4\n"),
    )

    def member(name, payload):
        return {"name": name, "artifact": ArtifactStore.reference(payload)}

    checker_source = b"reviewed input checker"
    manifest = DiagnosticBatchManifest.model_validate(
        fixture.manifest.model_dump()
        | {
            "cases": [
                {
                    "input": member(f"input-{i}", stdin),
                    "independent_expected": member(f"expected-{i}", expected),
                }
                for i, (stdin, expected) in enumerate(generated)
            ],
            "input_checker": {
                "constraints_revision": member("constraints", b"fixture constraints"),
                "source": member("checker", checker_source),
            },
            "reduction": {"original_input_sha256": ArtifactStore.reference(generated[0][0]).digest},
        }
    )
    assert manifest.searches_reduction
    config = fixture.configuration
    config = config.model_copy(
        update={
            "compile_limits": config.compile_limits.model_copy(
                update={"wall_time_us": 6_000_000, "cpu_time_us": 6_000_000}
            ),
            "run_limits": config.run_limits.model_copy(update={"wall_time_us": 1_000_000}),
            "job_wall_time_us": 50_000_000,
        }
    )
    checker = diagnostic_checker_configuration(
        config, "differential", len(generated), search_reduction=True
    )
    work = replace(
        fixture,
        manifest=manifest,
        configuration=config,
        cases=generated,
        input_check=DiagnosticInputCheck(checker, checker_source),
    )
    selected = min(ArtifactStore.reference(stdin).digest for stdin, _ in generated[1:3])
    seen = []

    async def observe(
        _batch, _worker, source, cases, _directory, deadline, _lock, _report, *, check_current
    ):
        seen.append((source, tuple(case.input.digest for case in cases), deadline))
        if source == checker_source:
            return SimpleNamespace(cases=[SimpleNamespace(outcome="completed") for _ in cases])
        if source == _REFERENCE:
            return SimpleNamespace(verdict=v1.Verdict.ACCEPTED)
        verdicts = [v1.Verdict.WRONG_ANSWER for _ in cases]
        if len(seen) == 3 or not stable:
            verdicts[-1] = v1.Verdict.ACCEPTED
        return SimpleNamespace(
            verdict=v1.Verdict.WRONG_ANSWER,
            cases=[
                SimpleNamespace(case_id=case.case_id, verdict=verdict)
                for case, verdict in zip(cases, verdicts, strict=True)
            ],
        )

    monkeypatch.setattr(replay, "_replay_source", observe)
    receipt = asyncio.run(replay.replay_batch(work, _CANDIDATE, Broker(tmp_path, clean=True)))
    assert [stage[0] for stage in seen] == [checker_source, _REFERENCE, _CANDIDATE, _CANDIDATE]
    assert [stage[2] for stage in seen] == sorted({stage[2] for stage in seen})
    assert seen[-1][1] == (manifest.reduction.original_input_sha256, selected) * 2
    expected = "reduction_verified" if stable else "reduction_unverified"
    assert receipt["state"] == expected
    assert receipt.get("selected_input_sha256") == (selected if stable else None)
    owner_source = DiagnosticSourceReference(
        uuid4(), manifest.problem_version_id, "c17", ArtifactStore.reference(_CANDIDATE)
    )
    public = replay.batch_summary(work, owner_source, receipt).model_dump_json()
    assert expected in public and selected not in public


def test_boundary_measurement_takes_inputs_only_and_records_environment(monkeypatch, tmp_path):
    sources = []
    empty = ArtifactStore.reference(b"")

    async def run_source(
        _batch, _worker, source, cases, directory, _deadline, _lock, report_type, *, check_current
    ):
        sources.append(source)
        assert report_type is DiagnosticBoundaryReport
        assert not (directory / "artifacts" / empty.digest).exists()
        assert all(case.expected == empty for case in cases)
        return SimpleNamespace(
            cases=tuple(
                BoundaryMeasurement(
                    case_id=case.case_id,
                    outcome="completed",
                    cpu_time_us=1000,
                    memory_peak_bytes=2048,
                )
                for case in cases
            )
        )

    monkeypatch.setattr(replay, "_replay_source", run_source)
    result = asyncio.run(
        replay.replay_batch(batch(boundary=True), _CANDIDATE, Broker(tmp_path, clean=True))
    )
    assert sources == [_CANDIDATE]
    assert result["state"] == "boundary_observed"
    assert result["sampling"]["architecture"]
    assert result["sampling"]["factory_config_sha256"] == Broker.factory_config_sha256
    assert result["measurements"] == [
        {"outcome": "completed", "cpu_time_us": 1000, "memory_peak_bytes": 2048},
        {"outcome": "completed", "cpu_time_us": 1000, "memory_peak_bytes": 2048},
    ]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("valid", (True, False))
def test_input_checker_runs_before_reference_or_candidate(monkeypatch, tmp_path, valid):
    material = batch()
    config = material.configuration.model_copy(
        update={
            "job_wall_time_us": 55_000_000,
            "compile_limits": material.configuration.compile_limits.model_copy(
                update={"wall_time_us": 18_500_000 - FACTORY_STAGE_INTERVAL_US}
            ),
        }
    )
    check = diagnostic_checker_configuration(config, "differential", len(_CASES))
    assert check.job_wall_time_us == 5_500_000
    checker_source = b"independently approved validator"
    material = replace(
        material,
        configuration=config,
        input_check=DiagnosticInputCheck(check, checker_source),
    )
    stages = []

    async def run_source(
        job_batch, _worker, source, cases, _directory, deadline, _lock, report, *, check_current
    ):
        stages.append((source, deadline))
        if report is DiagnosticBoundaryReport:
            assert job_batch.configuration == check
            assert all(case.expected.bytes == 0 for case in cases)
            return SimpleNamespace(
                cases=[
                    SimpleNamespace(outcome="completed"),
                    SimpleNamespace(outcome="completed" if valid else "runtime_error"),
                ]
            )
        assert job_batch.configuration == config
        assert all(case.expected.bytes > 0 for case in cases)
        return SimpleNamespace(
            verdict=v1.Verdict.ACCEPTED, cases=[SimpleNamespace(verdict=v1.Verdict.ACCEPTED)]
        )

    monkeypatch.setattr(replay, "_replay_source", run_source)
    result = asyncio.run(replay.replay_batch(material, _CANDIDATE, Broker(tmp_path, clean=True)))
    if valid:
        assert [source for source, _ in stages] == [checker_source, _REFERENCE, _CANDIDATE]
        assert [deadline for _, deadline in stages] == sorted(deadline for _, deadline in stages)
        assert result["inputs_verified"] is True
    else:
        assert [source for source, _ in stages] == [checker_source]
        assert result["state"] == "input_not_verified"
        assert "inputs_verified" not in result
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("reproduced", (True, False))
def test_reduction_replays_the_original_and_smaller_input_twice(monkeypatch, tmp_path, reproduced):
    material = batch()
    raw = material.manifest.model_dump()
    original = b"-3 10\n"
    original_ref = ArtifactStore.reference(original)
    raw["cases"][0]["input"]["artifact"] = original_ref
    raw["input_checker"] = {
        "constraints_revision": {
            "name": "constraints",
            "artifact": ArtifactStore.reference(b"integer constraints"),
        },
        "source": {"name": "checker", "artifact": ArtifactStore.reference(b"checker source")},
    }
    raw["reduction"] = {
        "original_input_sha256": original_ref.digest,
        "smaller_input_sha256": raw["cases"][1]["input"]["artifact"]["digest"],
    }
    config = material.configuration.model_copy(
        update={
            "job_wall_time_us": 55_000_000,
            "compile_limits": material.configuration.compile_limits.model_copy(
                update={"wall_time_us": 15_500_000 - FACTORY_STAGE_INTERVAL_US}
            ),
        }
    )
    checker = diagnostic_checker_configuration(config, "differential", 2, repeat_candidate=True)
    material = replace(
        material,
        manifest=DiagnosticBatchManifest.model_validate(raw),
        configuration=config,
        cases=((original, _CASES[0][1]), _CASES[1]),
        input_check=DiagnosticInputCheck(checker, b"checker source"),
    )
    lengths = []

    async def run_source(
        _batch, _worker, source, cases, _directory, _deadline, _lock, report_type, *, check_current
    ):
        lengths.append(len(cases))
        if report_type is DiagnosticBoundaryReport:
            assert source == b"checker source"
            return SimpleNamespace(cases=[SimpleNamespace(outcome="completed")] * 2)
        if source == _REFERENCE:
            return SimpleNamespace(verdict=v1.Verdict.ACCEPTED)
        assert source == _CANDIDATE
        assert len({case.case_id for case in cases}) == 4
        assert cases[0].input == cases[2].input and cases[1].input == cases[3].input
        results = [SimpleNamespace(verdict=v1.Verdict.WRONG_ANSWER) for _ in cases]
        if not reproduced:
            results[-1].verdict = v1.Verdict.ACCEPTED
        return SimpleNamespace(verdict=v1.Verdict.WRONG_ANSWER, cases=results)

    monkeypatch.setattr(replay, "_replay_source", run_source)
    receipt = asyncio.run(replay.replay_batch(material, _CANDIDATE, Broker(tmp_path, clean=True)))
    assert lengths == [2, 2, 4]
    assert receipt["inputs_verified"] is True
    assert receipt["state"] == ("reduction_verified" if reproduced else "reduction_unverified")
    assert not list(tmp_path.iterdir())
