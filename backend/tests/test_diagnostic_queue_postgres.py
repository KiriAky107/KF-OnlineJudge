import asyncio
import secrets
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import connect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_submission_create import setup_app

from kf_oj.contracts.diagnostic import DiagnosticObservation
from kf_oj.contracts.judging import FailureCode, Verdict
from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.modules.learning import diagnostic_queue
from kf_oj.modules.learning.diagnostic_inputs import DiagnosticInputsUnavailable
from kf_oj.modules.learning.diagnostic_queue import (
    DiagnosticBusy,
    DiagnosticQueue,
    DiagnosticRequestConflict,
)


def test_public_sample_diagnostics_admit_and_claim_separately(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        monkeypatch.setattr(diagnostic_queue, "_MAX_QUEUED", 1)
        async with app.router.lifespan_context(app):
            identity = app.state.identity

            async def create_actor(name):
                password = secrets.token_urlsafe(24)
                await identity.create_account(name, password)
                return await identity.login(name, password, peer="127.0.0.1"), password

            version = await app.state.problems.import_directory(tmp_path / "import")
            code = "int main(void) { return 0; }\n"
            owner, owner_password = await create_actor("queue_owner")
            other, _ = await create_actor("queue_other")
            next_actor, _ = await create_actor("queue_next")

            async def submit(actor):
                return await app.state.submissions.create(
                    actor.principal,
                    CreateSubmission(
                        problem_version_id=version,
                        language="c17",
                        source=code,
                        idempotency_key=uuid4(),
                    ),
                )

            original = await submit(owner)
            other_submission = await submit(other)
            next_submission = await submit(next_actor)
            operator = await connect(postgres_url)
            try:
                # Run the queue as the restricted platform role, including the proof recheck.
                async with engine.connect() as connection:
                    await connection.execute(text("SET ROLE kf_oj_platform"))
                    await connection.commit()
                    restricted = async_sessionmaker(connection, expire_on_commit=False)
                    platform_queue = DiagnosticQueue(restricted, identity)
                    queue = DiagnosticQueue(app.state.sessions, identity)

                    async def enqueue(request_id, actor=owner, submission=original, *, store=queue):
                        return await store.enqueue(
                            actor.principal,
                            request_id=request_id,
                            submission_id=submission.submission_id,
                            problem_version_id=version,
                            sample_ordinal=0,
                            source_execution_confirmed=True,
                        )

                    first = uuid4()
                    a, b = await asyncio.gather(enqueue(first), enqueue(first))
                    assert a == b and a.state == "queued"
                    saved = await operator.fetchrow(
                        "SELECT * FROM learning_diagnostic_requests WHERE id = $1", first
                    )
                    assert saved["source_digest"] == sha256(code.encode()).hexdigest()
                    assert saved["input_digest"] == sha256(b"1\n").hexdigest()
                    assert saved["expected_digest"] == sha256(b"2\n").hexdigest()
                    assert saved["session_proof"] != owner.principal.session_token_hash
                    assert (saved["input_bytes"], saved["expected_bytes"]) == (2, 2)
                    assert not await operator.fetchval(
                        "SELECT has_table_privilege('kf_oj_learning_worker', "
                        "'learning_diagnostic_requests', 'SELECT')"
                    )
                    assert not await operator.fetchval(
                        "SELECT has_table_privilege('kf_oj_coordinator', "
                        "'learning_diagnostic_requests', 'SELECT')"
                    )

                    with pytest.raises(DiagnosticBusy):
                        await enqueue(uuid4(), other, other_submission, store=platform_queue)
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await platform_queue.cancel(other.principal, first)
                    assert (await platform_queue.cancel(owner.principal, first)).state == (
                        "cancelled"
                    )
                    assert (await enqueue(first, store=platform_queue)).state == "cancelled"

                    expired = uuid4()
                    await enqueue(expired, store=platform_queue)
                    await operator.execute(
                        "UPDATE learning_diagnostic_requests SET created_at=clock_timestamp()"
                        "-interval '3 minutes', "
                        "queue_deadline=clock_timestamp()-interval '1 minute'"
                        " WHERE id=$1",
                        expired,
                    )
                    assert await platform_queue.claim() is None
                    assert (await enqueue(expired, store=platform_queue)).state == "expired"

                    revoked = uuid4()
                    await enqueue(revoked, store=platform_queue)
                    await identity.logout(owner.token, owner.csrf_token)
                    assert await platform_queue.claim() is None
                    assert (
                        await operator.fetchval(
                            "SELECT state FROM learning_diagnostic_requests WHERE id=$1", revoked
                        )
                        == "unavailable"
                    )
                    relogged = await identity.login(
                        owner.principal.username, owner_password, peer="127.0.0.1"
                    )
                    with pytest.raises(DiagnosticRequestConflict):
                        await enqueue(revoked, relogged, store=platform_queue)
                    with pytest.raises(DiagnosticBusy):
                        await enqueue(uuid4(), relogged, store=platform_queue)

                    changed = uuid4()
                    await enqueue(changed, other, other_submission, store=platform_queue)
                    await operator.execute(
                        "UPDATE learning_diagnostic_requests SET input_digest=repeat('0', 64)"
                        " WHERE id=$1",
                        changed,
                    )
                    assert await platform_queue.claim() is None
                    assert (
                        await operator.fetchval(
                            "SELECT state FROM learning_diagnostic_requests WHERE id=$1", changed
                        )
                        == "unavailable"
                    )

                    ready = uuid4()
                    await enqueue(ready, other, other_submission, store=platform_queue)
                    claim, duplicate = await asyncio.gather(platform_queue.claim(), queue.claim())
                    assert sorted(item is not None for item in (claim, duplicate)) == [False, True]
                    accepted = claim or duplicate
                    assert accepted.request_id == ready
                    assert accepted.inputs.account_id == other.principal.account_id
                    assert accepted.inputs.sample.input_bytes == b"1\n"
                    assert accepted.inputs.sample.expected_bytes == b"2\n"
                    assert await platform_queue.claim_current(accepted)
                    fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
                    admission = AdmissionRegistry.model_validate_json(
                        (fixtures / "judge-registry.valid.json").read_bytes()
                    )
                    problems = app.state.problems
                    with closing(ArtifactStore(tmp_path / "empty-archive")) as unavailable:
                        with pytest.raises(DiagnosticInputsUnavailable):
                            await platform_queue.execution_inputs(
                                accepted, ArtifactRepository(unavailable), problems, admission
                            )
                    sealed = app.state.submissions.artifacts
                    with pytest.raises(DiagnosticInputsUnavailable):
                        await platform_queue.execution_inputs(
                            replace(accepted, execution_id=uuid4()), sealed, problems, admission
                        )
                    with pytest.raises(DiagnosticInputsUnavailable, match="exact diagnostic"):
                        await platform_queue.execution_inputs(accepted, sealed, problems, None)
                    bundle = await platform_queue.execution_inputs(
                        accepted, sealed, problems, admission
                    )
                    assert (
                        bundle.configuration.toolchain
                        == admission.multi_runtime.capabilities[0].toolchain
                    )
                    assert bundle.configuration.job_wall_time_us <= 55_000_000
                    assert bundle.source == code.encode() and bundle.inputs == accepted.inputs
                    assert code.strip() not in repr(bundle)
                    following = uuid4()
                    await enqueue(following, next_actor, next_submission, store=platform_queue)
                    await operator.execute(
                        "UPDATE learning_diagnostic_requests SET execution_deadline="
                        "clock_timestamp()-interval '1 second' WHERE id=$1",
                        ready,
                    )
                    assert await platform_queue.claim() is None
                    assert (
                        await operator.fetchval(
                            "SELECT state FROM learning_diagnostic_requests WHERE id=$1", ready
                        )
                        == "running"
                    )
                    assert not await platform_queue.claim_current(accepted)
                    with pytest.raises(ValueError, match="cleanup"):
                        await platform_queue.acknowledge_stopped(
                            accepted, failure_code=FailureCode.CLEANUP_FAILED
                        )
                    with pytest.raises(DiagnosticRequestConflict):
                        await platform_queue.acknowledge_stopped(
                            replace(accepted, execution_id=uuid4()),
                            failure_code=FailureCode.JOB_DEADLINE,
                        )
                    assert await platform_queue.claim() is None
                    timed_out = await platform_queue.acknowledge_stopped(
                        accepted, failure_code=FailureCode.JOB_DEADLINE
                    )
                    assert timed_out.state == "expired"
                    assert (await platform_queue.acknowledge_stopped(accepted)) == timed_out

                    waiting = await platform_queue.claim()
                    assert waiting.request_id == following
                    assert (await platform_queue.cancel(next_actor.principal, following)).state == (
                        "cancel_pending"
                    )
                    assert not await platform_queue.claim_current(waiting)
                    assert await platform_queue.claim() is None
                    assert (await platform_queue.acknowledge_stopped(waiting)).state == "cancelled"

                    observed = uuid4()
                    await enqueue(observed, other, other_submission, store=platform_queue)
                    observation = DiagnosticObservation(
                        o2=Verdict.WRONG_ANSWER, o0=Verdict.ACCEPTED
                    )
                    assert (
                        await platform_queue.acknowledge_stopped(
                            await platform_queue.claim(), observation=observation
                        )
                    ).state == "observed"
                    row = await operator.fetchrow(
                        "SELECT o2,o0 FROM learning_diagnostic_requests WHERE id=$1", observed
                    )
                    assert tuple(row) == ("wrong_answer", "accepted")

                    withdrawn = uuid4()
                    await enqueue(withdrawn, next_actor, next_submission, store=platform_queue)
                    stale = await platform_queue.claim()
                    await identity.logout(next_actor.token, next_actor.csrf_token)
                    assert not await platform_queue.claim_current(stale)
                    assert (
                        await platform_queue.acknowledge_stopped(stale, observation=observation)
                    ).state == "unavailable"
                    row = await operator.fetchrow(
                        "SELECT o2,o0 FROM learning_diagnostic_requests WHERE id=$1", withdrawn
                    )
                    assert tuple(row) == (None, None)
                    await connection.execute(text("RESET ROLE"))
                    await connection.commit()
            finally:
                await operator.close()

    asyncio.run(exercise())
