import asyncio
import json
import secrets
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import connect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import platform_role
from test_submission_create import setup_app

from kf_oj.contracts.diagnostic_batch import (
    BatchDiagnosticSummary,
    BoundaryMeasurement,
    ConfirmBatchDiagnostic,
    RegisteredBatchOffer,
)
from kf_oj.contracts.judging import FailureCode
from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings
from kf_oj.modules.learning import diagnostic_queue
from kf_oj.modules.learning.diagnostic_inputs import DiagnosticInputsUnavailable
from kf_oj.modules.learning.diagnostic_queue import (
    BatchDiagnosticClaim,
    DiagnosticBusy,
    DiagnosticExecutorSettings,
    DiagnosticQueue,
    DiagnosticRequestConflict,
)
from kf_oj.workers import diagnostic, diagnostic_batch_replay
from kf_oj.workers.diagnostic import (
    DiagnosticCleanupUnconfirmed,
    DiagnosticWorker,
    _main,
    dispatch_once,
)
from kf_oj.workers.diagnostic_batch import preflight_registered_batch


def registered_boundary(tmp_path, version):
    files = {
        "generator": b"fixture generation revision 7",
        "input-a": b"3\n",
        "input-b": b"345\n",
    }

    def reference(name):
        payload = files[name]
        return {
            "name": name,
            "artifact": {"digest": sha256(payload).hexdigest(), "bytes": len(payload)},
        }

    manifest = json.dumps(
        {
            "schema_version": 1,
            "kind": "diagnostic.batch-inputs",
            "problem_version_id": str(version),
            "language": "c17",
            "purpose": "boundary",
            "generation": {
                "recipe": "fixed-test-values",
                "revision": reference("generator"),
                "seed": 7,
            },
            "cases": [{"input": reference(name)} for name in ("input-a", "input-b")],
        }
    ).encode()
    digest = sha256(manifest).hexdigest()
    registry = tmp_path / "registered"
    registry.mkdir(mode=0o700)
    package = registry / digest
    package.mkdir(mode=0o700)
    for name, payload in files.items():
        (package / name).write_bytes(payload)
    (package / "diagnostic-batch.json").write_bytes(manifest)
    return registry, digest


def test_registered_batches_share_the_sample_slot_and_recheck_evidence(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        monkeypatch.setattr(diagnostic_queue, "_MAX_ACCOUNT_PER_HOUR", 2)
        async with app.router.lifespan_context(app):
            identity = app.state.identity
            version = await app.state.problems.import_directory(tmp_path / "import")
            code = "int main(void) { return 0; }\n"
            registry, digest = registered_boundary(tmp_path, version)
            fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
            admission = AdmissionRegistry.model_validate_json(
                (fixtures / "judge-registry.valid.json").read_bytes()
            )
            problems = app.state.problems
            artifacts = app.state.submissions.artifacts

            async def load(db, pin):
                return await preflight_registered_batch(
                    db, problems, artifacts, admission, registry, pin
                )

            async def actor(name):
                password = secrets.token_urlsafe(24)
                await identity.create_account(name, password)
                session = await identity.login(name, password, peer="127.0.0.1")
                submission = await app.state.submissions.create(
                    session.principal,
                    CreateSubmission(
                        problem_version_id=version,
                        language="c17",
                        source=code,
                        idempotency_key=uuid4(),
                    ),
                )
                return session, submission.submission_id

            owner, submission_id = await actor("batch_queue_owner")
            other, other_submission = await actor("batch_queue_other")
            third, third_submission = await actor("batch_queue_third")
            next_actor, next_submission = await actor("batch_queue_next")
            request = ConfirmBatchDiagnostic(
                submission_id=submission_id,
                problem_version_id=version,
                manifest_sha256=digest,
                source_execution_confirmed=True,
            )
            operator = await connect(postgres_url)
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SET ROLE kf_oj_platform"))
                    await connection.commit()
                    restricted = async_sessionmaker(connection, expire_on_commit=False)
                    queue = DiagnosticQueue(restricted, identity, load_batch=load)
                    parallel = DiagnosticQueue(app.state.sessions, identity, load_batch=load)
                    sample_only = DiagnosticQueue(app.state.sessions, identity)

                    async def sample(actor, submitted, key):
                        return await queue.enqueue(
                            actor.principal,
                            request_id=key,
                            submission_id=submitted,
                            problem_version_id=version,
                            sample_ordinal=0,
                            source_execution_confirmed=True,
                        )

                    first = uuid4()
                    queued = await queue.enqueue_batch(
                        owner.principal, request_id=first, request=request
                    )
                    assert await queue.read_batch(owner.principal, first) == queued
                    assert (
                        await queue.enqueue_batch(
                            owner.principal, request_id=first, request=request
                        )
                        == queued
                    )
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await queue.read(owner.principal, first)
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await queue.read_batch(other.principal, first)
                    with pytest.raises(DiagnosticRequestConflict):
                        await sample(owner, submission_id, first)
                    with pytest.raises(DiagnosticBusy):
                        await sample(owner, submission_id, uuid4())
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await queue.enqueue_batch(
                            other.principal, request_id=uuid4(), request=request
                        )
                    saved = await operator.fetchrow(
                        "SELECT * FROM learning_diagnostic_requests WHERE id = $1", first
                    )
                    assert saved["mode"] == "batch" and saved["batch_manifest_digest"] == digest
                    assert saved["sample_ordinal"] is None and saved["input_bytes"] is None
                    assert saved["session_proof"] != owner.principal.session_token_hash

                    second = uuid4()
                    await sample(other, other_submission, second)
                    scope = uuid4()
                    a, b = await asyncio.gather(queue.claim(scope), parallel.claim(scope))
                    assert (a is None) != (b is None)
                    claim = a or b
                    assert isinstance(claim, BatchDiagnosticClaim) and claim.request_id == first
                    assert await sample_only.claim() is None
                    assert await queue.claim_current(claim)

                    async def withdrawn_registry(*_args):
                        raise AssertionError("a withdrawn batch must not read the registry")

                    withdrawn = DiagnosticQueue(
                        restricted, identity, batch_offers=(), load_batch=withdrawn_registry
                    )
                    assert not await withdrawn.claim_current(claim)
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await withdrawn.batch_execution_inputs(claim, artifacts)

                    original = registry / digest / "input-a"
                    original.write_bytes(b"4\n")
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await queue.batch_execution_inputs(claim, artifacts)
                    original.write_bytes(b"3\n")
                    environment = DiagnosticExecutorSettings(
                        factory_config_sha256="b" * 64, admission_sha256="c" * 64
                    )
                    bundle = await queue.batch_execution_inputs(
                        claim, artifacts, executor=environment
                    )
                    assert bundle.source == code.encode()
                    assert bundle.batch.cases == ((b"3\n", None), (b"345\n", None))
                    assert (
                        await queue.batch_execution_inputs(claim, artifacts, executor=environment)
                        == bundle
                    )
                    with pytest.raises(DiagnosticInputsUnavailable, match="configuration changed"):
                        await queue.batch_execution_inputs(
                            claim,
                            artifacts,
                            executor=environment.model_copy(
                                update={"factory_config_sha256": "d" * 64}
                            ),
                        )

                    assert (await queue.cancel_batch(owner.principal, first)).state == (
                        "cancel_pending"
                    )
                    assert not await queue.claim_current(claim)
                    with pytest.raises(ValueError, match="cleanup"):
                        await queue.acknowledge_batch_stopped(
                            claim, failure_code=FailureCode.CLEANUP_FAILED
                        )
                    assert await sample_only.claim() is None
                    assert (await queue.acknowledge_batch_stopped(claim)).state == "cancelled"
                    sample_claim = await sample_only.claim(scope)
                    assert sample_claim.request_id == second
                    await operator.execute(
                        "UPDATE learning_diagnostic_requests SET execution_deadline="
                        "clock_timestamp()-interval '1 second' WHERE id=$1",
                        second,
                    )

                    async def confirmed():
                        return True

                    assert await queue.recover_expired(uuid4(), confirmed) is None
                    assert (await queue.recover_expired(scope, confirmed)).state == "expired"
                    next_batch = uuid4()
                    await queue.enqueue_batch(
                        owner.principal, request_id=next_batch, request=request
                    )
                    batch_claim = await queue.claim(scope)
                    summary = BatchDiagnosticSummary(
                        schema_version=1,
                        submission_id=submission_id,
                        problem_version_id=version,
                        purpose="boundary",
                        outcome="resource_limit",
                    )
                    with pytest.raises(ValueError, match="outcome"):
                        await queue.acknowledge_batch_stopped(
                            batch_claim,
                            summary=summary.model_copy(update={"submission_id": uuid4()}),
                        )
                    assert (
                        await queue.acknowledge_batch_stopped(batch_claim, summary=summary)
                    ).summary == summary
                    assert (await queue.read_batch(owner.principal, next_batch)).summary == summary
                    with pytest.raises(DiagnosticBusy):
                        await sample(owner, submission_id, uuid4())

                    revoked = uuid4()
                    await queue.enqueue_batch(
                        third.principal,
                        request_id=revoked,
                        request=request.model_copy(update={"submission_id": third_submission}),
                    )
                    await identity.logout(third.token, third.csrf_token)
                    assert await queue.claim(scope) is None
                    assert (
                        await operator.fetchval(
                            "SELECT state FROM learning_diagnostic_requests WHERE id=$1", revoked
                        )
                        == "unavailable"
                    )
                    with pytest.raises(DiagnosticRequestConflict):
                        await queue.acknowledge_batch_stopped(
                            replace(batch_claim, execution_id=uuid4()), summary=summary
                        )

                    uncertain = uuid4()
                    await queue.enqueue_batch(
                        other.principal,
                        request_id=uncertain,
                        request=request.model_copy(update={"submission_id": other_submission}),
                    )
                    staging = tmp_path / "diagnostic-stage"
                    staging.mkdir(mode=0o700)
                    clean = True
                    interrupt = True

                    class Broker(DiagnosticWorker):
                        def admission(self):
                            return admission

                        async def confirm_cleanup(self, lock_fd):
                            assert lock_fd >= 0
                            return clean

                    worker = Broker(
                        binary=tmp_path / "unused-worker",
                        factory_socket=tmp_path / "factory",
                        staging=staging,
                        admission_file=tmp_path / "admission",
                        worker_id=uuid4(),
                        factory_config_sha256="a" * 64,
                    )
                    settings = Settings(
                        _env_file=None,
                        environment="test",
                        database_url=postgres_url,
                        artifact_root=tmp_path / "artifacts",
                        judge_admission=admission,
                        learning_diagnostics_enabled=True,
                        learning_diagnostic_batch_offers=(
                            RegisteredBatchOffer(
                                manifest_sha256=digest,
                                problem_version_id=version,
                                language="c17",
                                purpose="boundary",
                                title="已登记的边界数据",
                            ),
                        ),
                    )

                    def broker_engine(_):
                        engine = create_async_engine(postgres_url)
                        platform_role(engine)
                        return engine

                    monkeypatch.setattr(diagnostic, "build_engine", broker_engine)
                    await _main(settings, worker, registry, mode="check")
                    assert (await queue.read_batch(other.principal, uncertain)).state == "queued"
                    await operator.execute(
                        "REVOKE UPDATE (execution_profile) ON learning_diagnostic_requests "
                        "FROM kf_oj_platform"
                    )
                    try:
                        with pytest.raises(ValueError, match="cannot claim tasks and publish"):
                            await _main(settings, worker, registry, mode="check")
                        assert (
                            await queue.read_batch(other.principal, uncertain)
                        ).state == "queued"
                    finally:
                        await operator.execute(
                            "GRANT UPDATE (execution_profile) ON learning_diagnostic_requests "
                            "TO kf_oj_platform"
                        )
                    wrong_offer = settings.learning_diagnostic_batch_offers[0].model_copy(
                        update={"language": "cpp20"}
                    )
                    with pytest.raises(ValueError, match="differs from its registry"):
                        await _main(
                            settings.model_copy(
                                update={"learning_diagnostic_batch_offers": (wrong_offer,)}
                            ),
                            worker,
                            registry,
                            mode="check",
                        )
                    assert (await queue.read_batch(other.principal, uncertain)).state == "queued"

                    async def observe(
                        batch,
                        _worker,
                        source,
                        cases,
                        directory,
                        deadline,
                        _lock_fd,
                        _report_type,
                        *,
                        check_current,
                    ):
                        assert source == code.encode() and await check_current()
                        assert 0 < deadline - asyncio.get_running_loop().time() <= 55
                        assert (
                            directory / "artifacts" / cases[0].input.digest
                        ).read_bytes() == b"3\n"
                        if interrupt:
                            raise DiagnosticCleanupUnconfirmed()
                        return SimpleNamespace(
                            cases=[
                                BoundaryMeasurement(
                                    case_id=case.case_id,
                                    outcome="completed",
                                    cpu_time_us=12,
                                    memory_peak_bytes=4096,
                                )
                                for case in cases
                            ]
                        )

                    monkeypatch.setattr(diagnostic_batch_replay, "_replay_source", observe)
                    with pytest.raises(DiagnosticCleanupUnconfirmed):
                        await dispatch_once(queue, artifacts, problems, worker)
                    prepared = list(tmp_path.glob(".kf-diagnostic-batch-*"))
                    assert len(prepared) == 1
                    assert (prepared[0] / "artifacts").is_dir()
                    await operator.execute(
                        "UPDATE learning_diagnostic_requests SET execution_deadline="
                        "clock_timestamp()-interval '1 second' WHERE id=$1",
                        uncertain,
                    )
                    clean = False
                    assert await dispatch_once(queue, artifacts, problems, worker) is None
                    assert prepared[0].exists()
                    clean = True
                    recovered = await dispatch_once(queue, artifacts, problems, worker)
                    assert recovered.request_id == uncertain and recovered.state == "expired"
                    assert not prepared[0].exists()

                    interrupt = False
                    next_key = uuid4()
                    await queue.enqueue_batch(
                        next_actor.principal,
                        request_id=next_key,
                        request=request.model_copy(update={"submission_id": next_submission}),
                    )
                    completed = await dispatch_once(queue, artifacts, problems, worker)
                    assert completed.request_id == next_key and completed.state == "observed"
                    assert completed.summary.outcome == "completed"
                    recorded = await operator.fetchrow(
                        "SELECT execution_profile, execution_started_at, finished_at "
                        "FROM learning_diagnostic_requests WHERE id=$1",
                        next_key,
                    )
                    profile = json.loads(recorded["execution_profile"])
                    assert profile["tool"] == "registered-batch-v1"
                    assert profile["factory_config_sha256"] == worker.factory_config_sha256
                    assert (
                        profile["admission_sha256"]
                        == sha256(admission.model_dump_json().encode()).hexdigest()
                    )
                    assert set(profile) == {
                        "tool",
                        "configuration_sha256",
                        "factory_config_sha256",
                        "admission_sha256",
                        "sandbox_wall_budget_us",
                    }
                    assert recorded["finished_at"] >= recorded["execution_started_at"]
                    assert not list(tmp_path.glob(".kf-diagnostic-batch-*"))

                    withdrawn_key = uuid4()
                    await queue.enqueue_batch(
                        next_actor.principal,
                        request_id=withdrawn_key,
                        request=request.model_copy(update={"submission_id": next_submission}),
                    )

                    async def dispatch_one(dispatch, _stop):
                        assert await dispatch() is None

                    monkeypatch.setattr(diagnostic, "_serve", dispatch_one)
                    monkeypatch.setattr(
                        diagnostic, "preflight_registered_batch", withdrawn_registry
                    )
                    await _main(
                        settings.model_copy(update={"learning_diagnostic_batch_offers": ()}),
                        worker,
                        registry,
                        mode="serve",
                    )
                    stopped = await operator.fetchrow(
                        "SELECT state, execution_id, batch_outcome "
                        "FROM learning_diagnostic_requests WHERE id=$1",
                        withdrawn_key,
                    )
                    assert tuple(stopped) == ("unavailable", None, None)
            finally:
                await operator.close()

    asyncio.run(exercise())
