import asyncio
import json
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import connect
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings
from kf_oj.modules.learning.diagnostic_queue import DiagnosticQueue, DiagnosticRequest
from kf_oj.workers.diagnostic import (
    DiagnosticCleanupUnconfirmed,
    DiagnosticWorker,
    _serve,
    dispatch_once,
)


def test_diagnostic_broker_recovers_only_the_expired_slot_of_its_original_factory(
    postgres_url, tmp_path, monkeypatch
):
    binary = os.environ.get("KF_OJ_TEST_DIAGNOSTIC_WORKER")
    if binary is None:
        pytest.skip("real diagnostic broker requires KF_OJ_TEST_DIAGNOSTIC_WORKER")

    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            identity = app.state.identity
            password = secrets.token_urlsafe(24)
            await identity.create_account("diagnostic_dispatch", password)
            issued = await identity.login("diagnostic_dispatch", password, peer="127.0.0.1")
            version = await app.state.problems.import_directory(tmp_path / "import")
            submission = await app.state.submissions.create(
                issued.principal,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    source="int main(void) { return 0; }\n",
                    idempotency_key=uuid4(),
                ),
            )
            queue = DiagnosticQueue(app.state.sessions, identity)
            fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
            admission = tmp_path / "admission.json"
            admission.touch(mode=0o600)
            admission.write_bytes((fixtures / "judge-registry.valid.json").read_bytes())
            worker = DiagnosticWorker(
                binary=Path(binary),
                factory_socket=tmp_path / "absent.sock",
                staging=tmp_path / "staging",
                admission_file=admission,
                worker_id=uuid4(),
                factory_config_sha256="b" * 64,
            )
            worker.staging.mkdir(mode=0o700)
            artifacts = app.state.submissions.artifacts
            problems = app.state.problems

            async def enqueue(actor=issued, own_submission=submission):
                return await queue.enqueue(
                    actor.principal,
                    request_id=uuid4(),
                    submission_id=own_submission.submission_id,
                    problem_version_id=version,
                    sample_ordinal=0,
                    source_execution_confirmed=True,
                )

            queued = await enqueue()
            settings = Settings(
                _env_file=None,
                environment="test",
                database_url=postgres_url,
                artifact_root=tmp_path / "inspection-artifacts",
                learning_diagnostics_enabled=True,
            )
            preflight = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "kf_oj.workers.diagnostic",
                    "--check",
                    "--binary",
                    str(worker.binary),
                    "--factory-socket",
                    str(worker.factory_socket),
                    "--staging",
                    str(worker.staging),
                    "--admission-file",
                    str(worker.admission_file),
                    "--worker-id",
                    str(worker.worker_id),
                    "--factory-config-sha256",
                    worker.factory_config_sha256,
                ],
                env=os.environ
                | {
                    "KF_OJ_ENVIRONMENT": "test",
                    "KF_OJ_DATABASE_URL": postgres_url,
                    "KF_OJ_ARTIFACT_ROOT": str(settings.artifact_root),
                    "KF_OJ_LEARNING_DIAGNOSTICS_ENABLED": "true",
                    "KF_OJ_LEARNING_DIAGNOSTIC_BATCH_OFFERS": "[]",
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert preflight.returncode == 1 and not preflight.stdout
            assert (
                preflight.stderr.strip()
                == "diagnostic startup: diagnostic factory readiness is unconfirmed"
            )
            assert (await queue.read(issued.principal, queued.request_id)).state == "queued"

            result = await dispatch_once(queue, artifacts, problems, worker)
            assert (result.request_id, result.state) == (queued.request_id, "failed")
            db = await connect(postgres_url)
            try:
                timing = await db.fetchrow(
                    "SELECT failure_code, execution_profile, execution_started_at, "
                    "execution_deadline, finished_at FROM learning_diagnostic_requests WHERE id=$1",
                    queued.request_id,
                )
                assert timing["failure_code"] == "sandbox_unavailable"
                profile = json.loads(timing["execution_profile"])
                assert profile["tool"] == "public-sample-v1"
                assert profile["factory_config_sha256"] == worker.factory_config_sha256
                assert (
                    profile["admission_sha256"]
                    == sha256(worker.admission().model_dump_json().encode()).hexdigest()
                )
                assert 0 < profile["sandbox_wall_budget_us"] <= 55_000_000
                assert timing["execution_started_at"] <= timing["finished_at"]
                assert timing["finished_at"] < timing["execution_deadline"]
                assert not list(worker.staging.iterdir())
                queued = await enqueue()
                crashing = replace(worker, binary=Path("/usr/bin/false"))
                with pytest.raises(DiagnosticCleanupUnconfirmed):
                    await dispatch_once(queue, artifacts, problems, crashing)
                state = await db.fetchval(
                    "SELECT state FROM learning_diagnostic_requests WHERE id=$1", queued.request_id
                )
                assert state == "running" and await queue.claim() is None
                assert not list(worker.staging.iterdir())

                next_password = secrets.token_urlsafe(24)
                await identity.create_account("diagnostic_dispatch_next", next_password)
                next_actor = await identity.login(
                    "diagnostic_dispatch_next", next_password, peer="127.0.0.1"
                )
                next_submission = await app.state.submissions.create(
                    next_actor.principal,
                    CreateSubmission(
                        problem_version_id=version,
                        language="c17",
                        source="int main(void) { return 0; }\n",
                        idempotency_key=uuid4(),
                    ),
                )
                next_request = await enqueue(next_actor, next_submission)
                assert await dispatch_once(queue, artifacts, problems, worker) is None
                await db.execute(
                    "UPDATE learning_diagnostic_requests"
                    " SET execution_deadline=clock_timestamp()-interval '1 second' WHERE id=$1",
                    queued.request_id,
                )
                assert await dispatch_once(queue, artifacts, problems, worker) is None

                calls = []

                async def clean(original, fd):
                    assert os.fstat(fd).st_ino == original.staging.stat().st_ino
                    calls.append(original)
                    # Recovery cannot run concurrently with a second broker on this staging.
                    with original.execution_lock() as competing:
                        assert competing is None
                    return True

                monkeypatch.setattr(DiagnosticWorker, "confirm_cleanup", clean)
                different_factory = replace(worker, factory_config_sha256="c" * 64)
                assert await dispatch_once(queue, artifacts, problems, different_factory) is None
                assert calls == []
                with worker.execution_lock() as active:
                    assert active is not None
                    assert await dispatch_once(queue, artifacts, problems, worker) is None
                assert calls == []
                recovered = await dispatch_once(queue, artifacts, problems, worker)
                assert (recovered.request_id, recovered.state) == (queued.request_id, "expired")
                assert calls == [worker]
                next_result = await dispatch_once(queue, artifacts, problems, worker)
                assert (next_result.request_id, next_result.state) == (
                    next_request.request_id,
                    "failed",
                )
                assert calls == [worker]

                cancelled = await enqueue()
                with pytest.raises(DiagnosticCleanupUnconfirmed):
                    await dispatch_once(queue, artifacts, problems, crashing)
                assert (await queue.cancel(issued.principal, cancelled.request_id)).state == (
                    "cancel_pending"
                )
                await db.execute(
                    "UPDATE learning_diagnostic_requests"
                    " SET execution_deadline=clock_timestamp()-interval '1 second' WHERE id=$1",
                    cancelled.request_id,
                )
                recovered = await dispatch_once(queue, artifacts, problems, worker)
                assert (recovered.request_id, recovered.state) == (
                    cancelled.request_id,
                    "cancelled",
                )
                assert calls == [worker, worker]
            finally:
                await db.close()

    asyncio.run(exercise())


def test_diagnostic_broker_lock_stays_with_its_subprocess(tmp_path):
    worker = DiagnosticWorker(
        binary=Path(sys.executable),
        factory_socket=tmp_path / "factory.sock",
        staging=tmp_path,
        admission_file=tmp_path / "admission.json",
        worker_id=uuid4(),
        factory_config_sha256="b" * 64,
    )
    tmp_path.chmod(0o700)
    with worker.execution_lock() as lease:
        assert lease is not None
        fd, original_scope = lease
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE,
            pass_fds=(fd,),
        )
    try:
        with worker.execution_lock() as held:
            assert held is None
    finally:
        child.communicate(input=b"x", timeout=5)
    with worker.execution_lock() as next_lease:
        assert next_lease is not None
        assert next_lease[1] == original_scope


def test_diagnostic_service_drains_the_current_task_when_stopping(capsys):
    async def exercise():
        stop = asyncio.Event()
        claimed = asyncio.Event()
        finish = asyncio.Event()
        calls = 0

        async def dispatch():
            nonlocal calls
            calls += 1
            claimed.set()
            await finish.wait()
            return DiagnosticRequest(
                request_id=uuid4(),
                state="observed",
                queue_deadline=datetime.now(UTC),
                mode="optimization",
                sample_ordinal=0,
            )

        running = asyncio.create_task(_serve(dispatch, stop))
        await claimed.wait()
        stop.set()
        await asyncio.sleep(0)
        assert not running.done()
        finish.set()
        await asyncio.wait_for(running, timeout=1)
        assert calls == 1

    asyncio.run(exercise())
    assert capsys.readouterr().out == "observed\n"
