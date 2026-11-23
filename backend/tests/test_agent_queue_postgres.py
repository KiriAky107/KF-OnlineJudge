"""Exercise learning run claim fencing and cancellation using real transactions."""

import asyncio
import secrets
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.agent_learning import AgentLearningInput
from kf_oj.contracts.agent_runtime import AgentPurpose, AgentStep, RunState
from kf_oj.modules.learning.agent_application import AgentLearning
from kf_oj.modules.learning.agent_queue import AgentQueue
from kf_oj.modules.learning.public import SUBMISSION_HISTORY, agent_storage
from kf_oj.runtime.storage import AgentRecordConflict, AgentRecordUnavailable


def test_real_claim_recovery_and_cancellation_fence(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        admin = create_async_engine(postgres_url)
        try:
            async with app.router.lifespan_context(app):
                identity = app.state.identity
                sessions = app.state.sessions
                learning = AgentLearning(sessions, identity)
                queue = AgentQueue(sessions)
                parallel = AgentQueue(sessions)

                async def actor(name):
                    password = secrets.token_urlsafe(24)
                    await identity.create_account(name, password)
                    return (await identity.login(name, password, peer="learning-queue")).principal

                owner = await actor("queue_owner")
                foreign = await actor("queue_other")
                storage = agent_storage(sessions, owner)
                purpose = AgentPurpose.SUBMISSION_REVIEW
                skill = learning.catalog.list_versions(purpose)[0]
                await learning.skills.configure(
                    owner,
                    skill.version_id,
                    parameters=b"{}",
                    enabled=True,
                    authorized_tools=frozenset({SUBMISSION_HISTORY}),
                )
                session = await learning.create_session(owner, purpose)

                async def enqueue():
                    return (
                        await learning.prepare(
                            owner,
                            session.session_id,
                            uuid4(),
                            skill.version_id,
                            AgentLearningInput(),
                        )
                    ).run.run_id

                pending = await enqueue()
                with pytest.raises(AgentRecordUnavailable):
                    await learning.cancel(foreign, pending)
                assert (await learning.cancel(owner, pending)).run.state == RunState.CANCELLED
                assert await queue.claim() is None

                deferred_id = await enqueue()
                active = await queue.claim()
                assert active is not None and active.run.run_id == deferred_id
                async with sessions.begin() as db:
                    deferred = await queue.defer(db, active.run)
                assert deferred is not None and deferred.state == RunState.RETRY_WAIT
                assert await queue.claim() is None
                async with admin.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE learning_agent_tasks SET available_at=clock_timestamp() "
                            "WHERE run_id=:run"
                        ),
                        {"run": deferred_id},
                    )
                retried = await queue.claim()
                assert retried is not None and retried.attempt == 2
                cancelled = await learning.cancel(owner, deferred_id)
                assert cancelled.run.state == RunState.CANCEL_REQUESTED
                assert await queue.acknowledge_cancelled(retried)

                run_id = await enqueue()
                claimed = await asyncio.gather(queue.claim(), parallel.claim())
                (lease,) = [claim for claim in claimed if claim is not None]
                assert lease.run.run_id == run_id and lease.attempt == 1
                assert await queue.renew(lease)
                async with admin.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE learning_agent_tasks SET lease_expires_at=clock_timestamp()"
                            "-interval '1 second' WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    )
                assert not await queue.renew(lease)
                step = AgentStep(
                    step_id=uuid4(),
                    run_id=run_id,
                    sequence=1,
                    kind="tool",
                    invocation_version="submission_history-v1",
                    arguments_digest="b" * 64,
                    state="running",
                )
                with pytest.raises(AgentRecordConflict, match="lease expired"):
                    await storage.record_step(step, lease.run.revision)
                replacement = await parallel.claim()
                assert replacement is not None and replacement.attempt == 2
                assert replacement.run.revision > lease.run.revision
                with pytest.raises(AgentRecordConflict):
                    await learning.execute_claimed(owner, run_id, lease.run.revision)
                await storage.record_step(step, replacement.run.revision)

                assert (await learning.cancel(owner, run_id)).run.state == RunState.CANCEL_REQUESTED
                assert not await parallel.renew(replacement)
                assert not await queue.acknowledge_cancelled(lease)
                assert await parallel.acknowledge_cancelled(replacement)
                outcome = await learning.execution(owner, run_id)
                assert outcome.run.state == RunState.CANCELLED and outcome.result is None
                assert (await storage.get_step(run_id, 1)).state == "cancelled"
                assert (await learning.cancel(owner, run_id)) == outcome
                assert await queue.claim() is None
        finally:
            await admin.dispose()

    asyncio.run(exercise())
