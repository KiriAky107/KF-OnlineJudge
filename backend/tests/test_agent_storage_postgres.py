"""Exercise the new Agent storage under real platform-role permissions."""

import asyncio
import secrets
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_identity import restrict_platform_role

from kf_oj.contracts.agent_runtime import AgentPurpose, AgentStep, RunState
from kf_oj.modules.identity.service import IdentityService
from kf_oj.modules.learning.public import agent_storage
from kf_oj.runtime.storage import AgentRecordConflict, AgentRecordUnavailable


def test_real_agent_storage_fences_retries_and_user_scope(postgres_url):
    async def run():
        admin = create_async_engine(postgres_url)
        platform = create_async_engine(postgres_url)
        restrict_platform_role(platform)
        sessions = async_sessionmaker(platform, expire_on_commit=False)
        identity = IdentityService(sessions)
        try:

            async def authenticated(name):
                password = secrets.token_urlsafe(24)
                await identity.create_account(name, password)
                login = await identity.login(name, password, peer="agent-storage-test")
                return await identity.authenticate(login.token)

            owner = await authenticated("agent_owner")
            a = agent_storage(sessions, owner)
            b = agent_storage(sessions, await authenticated("agent_other"))
            session = await a.create_session(AgentPurpose.GUIDED_HELP)
            assert session.owner_id == owner.account_id
            assert await a.get_session(session.session_id) == session
            with pytest.raises(AgentRecordUnavailable):
                await b.get_session(session.session_id)

            request = dict(
                request_id=uuid4(),
                skill_version_id=uuid4(),
                configuration_digest="a" * 64,
                input_digest="b" * 64,
            )
            with pytest.raises(AgentRecordUnavailable):
                await b.enqueue(session.session_id, **request)
            first, second = await asyncio.gather(
                a.enqueue(session.session_id, **request), a.enqueue(session.session_id, **request)
            )
            assert first == second and first.revision == 0 and first.state == RunState.QUEUED
            with pytest.raises(AgentRecordConflict):
                await a.enqueue(session.session_id, **(request | {"input_digest": "c" * 64}))
            with pytest.raises(AgentRecordUnavailable):
                await b.get_run(first.run_id)
            with pytest.raises(AgentRecordUnavailable):
                await b.transition(first.run_id, first.revision, RunState.CANCELLED)

            running = await a.transition(first.run_id, first.revision, RunState.RUNNING)
            with pytest.raises(AgentRecordConflict):
                await a.transition(first.run_id, first.revision, RunState.RUNNING)
            step = AgentStep(
                step_id=uuid4(),
                run_id=running.run_id,
                sequence=1,
                kind="tool",
                invocation_version="learning-overview-v1",
                arguments_digest="d" * 64,
                state="running",
            )
            with pytest.raises(AgentRecordUnavailable):
                await b.record_step(step, running.revision)
            assert await a.record_step(step, running.revision) == step
            with pytest.raises(AgentRecordUnavailable):
                await b.get_step(step.run_id, step.sequence)
            await a.transition(running.run_id, running.revision, RunState.CANCEL_REQUESTED)
            with pytest.raises(AgentRecordConflict):
                await a.record_step(step, running.revision)
            cancellation = await a.get_run(running.run_id)
            settled = step.model_copy(update={"state": "cancelled"})
            assert await a.record_step(settled, cancellation.revision) == settled
            assert await a.record_step(settled, cancellation.revision) == settled
            cancelled = await a.transition(
                cancellation.run_id, cancellation.revision, RunState.CANCELLED
            )
            assert await agent_storage(sessions, owner).get_run(first.run_id) == cancelled
            assert await a.get_step(step.run_id, step.sequence) == settled
            assert await a.enqueue(session.session_id, **request) == cancelled

            async with admin.connect() as connection:
                for table in ("sessions", "runs", "steps"):
                    assert not await connection.scalar(
                        text("SELECT has_table_privilege('kf_oj_coordinator', :table, 'SELECT')"),
                        {"table": f"learning_agent_{table}"},
                    )
                assert not await connection.scalar(
                    text("""SELECT has_column_privilege(
                        'kf_oj_platform', 'learning_agent_runs', 'skill_version_id', 'UPDATE'
                    )""")
                )
        finally:
            await platform.dispose()
            await admin.dispose()

    asyncio.run(run())
