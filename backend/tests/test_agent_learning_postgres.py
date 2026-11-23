"""Execute the four workflows with real isolated database state and owned evidence."""

import asyncio
import json
import secrets
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.agent_learning import AgentLearningInput
from kf_oj.contracts.agent_runtime import AgentPurpose, RunState
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning import agent_application
from kf_oj.modules.learning.agent_application import AgentLearning
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.learning.public import (
    AGENT_TOOL_SCOPES,
    TRAINING_CANDIDATES,
    LearningUnavailable,
    agent_storage,
)
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata
from kf_oj.runtime.storage import AgentRecordUnavailable
from kf_oj.runtime.tools import ToolKey
from kf_oj.workers.learning import verify_role


def test_real_learning_workflows_use_pinned_skill_and_owned_inputs(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        admin = create_async_engine(postgres_url)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        worker_sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
        try:
            await verify_role(worker_engine)
            with pytest.raises(ValueError, match="dedicated database role"):
                await verify_role(admin)
            async with app.router.lifespan_context(app):
                identity = app.state.identity
                sessions = app.state.sessions
                learning = AgentLearning(sessions, identity)
                problem_version = await app.state.problems.import_directory(tmp_path / "import")
                async with sessions.begin() as db:
                    db.add(
                        ProblemLearningMetadata(
                            problem_version_id=problem_version,
                            tags=["循环"],
                            difficulty="easy",
                            revision=1,
                        )
                    )

                async def login(username):
                    password = secrets.token_urlsafe(24)
                    await identity.create_account(username, password)
                    return await identity.login(username, password, peer="learning-workflow")

                async def submit(principal, verdict):
                    submission = await app.state.submissions.create(
                        principal,
                        CreateSubmission(
                            problem_version_id=problem_version,
                            language="c17",
                            idempotency_key=uuid4(),
                            source="synthetic fixture; never executed",
                        ),
                    )
                    if verdict is not None:
                        await finish(submission.submission_id, verdict)
                    return submission.submission_id

                async def finish(submission_id, verdict):
                    async with admin.begin() as db:
                        await db.execute(
                            text(
                                "UPDATE evaluations SET state='finished', verdict=:verdict, "
                                "updated_at=clock_timestamp() WHERE submission_id=:id"
                            ),
                            {"verdict": verdict, "id": submission_id},
                        )

                login_owner = await login("workflow_owner")
                owner = login_owner.principal
                foreign = (await login("workflow_other")).principal
                first = await submit(owner, "wrong_answer")
                passed = await submit(owner, "accepted")
                foreign_id = await submit(foreign, "runtime_error")
                storage = agent_storage(sessions, owner)
                worker = LearningWorker(worker_sessions)
                preferences = {
                    AgentPurpose.SUBMISSION_REVIEW: {"detail": "brief"},
                    AgentPurpose.GUIDED_HELP: {"hint_level": "location"},
                    AgentPurpose.FOCUSED_TRAINING: {
                        "tags": ["循环"],
                        "maximum_difficulty": "easy",
                        "daily_minutes": 20,
                    },
                    AgentPurpose.PERIODIC_REVIEW: {"cycle_days": 21, "sessions_per_week": 2},
                }

                async def run(purpose, *, submission=None, restart=False, after_enqueue=None):
                    skill = learning.catalog.list_versions(purpose)[0]
                    tools = (
                        frozenset(
                            ToolKey(item.name, item.version)
                            for item in skill.declaration.requested_tools
                        )
                        & AGENT_TOOL_SCOPES.keys()
                    )
                    await learning.skills.configure(
                        owner,
                        skill.version_id,
                        parameters=json.dumps(preferences[purpose]).encode(),
                        enabled=True,
                        authorized_tools=tools,
                    )
                    session = await learning.create_session(owner, purpose)
                    body = AgentLearningInput(submission_id=submission)
                    request = uuid4()
                    initial = await learning.prepare(
                        owner, session.session_id, request, skill.version_id, body
                    )
                    assert initial.run.state == RunState.QUEUED and initial.result is None
                    if after_enqueue is not None:
                        await after_enqueue()

                    if restart:
                        original_tools = agent_application.scoped_agent_tools

                        class StopAfterHistory:
                            def __init__(self, *args, **kwargs):
                                self.tools = original_tools(*args, **kwargs)

                            async def invoke(self, context, key, args):
                                if key == TRAINING_CANDIDATES:
                                    raise asyncio.CancelledError()
                                return await self.tools.invoke(context, key, args)

                        with monkeypatch.context() as interrupted:
                            interrupted.setattr(
                                agent_application, "scoped_agent_tools", StopAfterHistory
                            )
                            with pytest.raises(asyncio.CancelledError):
                                await worker.run_once()
                        assert (await storage.get_step(initial.run.run_id, 1)).state == "succeeded"
                        assert (await storage.get_step(initial.run.run_id, 2)).state == "running"
                        async with admin.begin() as db:
                            await db.execute(
                                text(
                                    "UPDATE learning_agent_tasks SET lease_expires_at="
                                    "clock_timestamp()"
                                    "-interval '1 second' WHERE run_id=:run"
                                ),
                                {"run": initial.run.run_id},
                            )
                    assert await worker.run_once()
                    outcome = await learning.execution(owner, initial.run.run_id)
                    assert (
                        await learning.prepare(
                            owner, session.session_id, request, skill.version_id, body
                        )
                        == outcome
                    )
                    with pytest.raises(AgentRecordUnavailable):
                        await learning.execution(foreign, initial.run.run_id)
                    return outcome

                review = await run(AgentPurpose.SUBMISSION_REVIEW)
                assert review.run.state == RunState.SUCCEEDED
                assert {item.submission_id for item in review.result.records} == {first, passed}
                (comparison,) = review.result.comparisons
                assert (comparison.earlier_submission_id, comparison.later_submission_id) == (
                    first,
                    passed,
                )
                assert comparison.outcome == "later_accepted"
                assert (await storage.get_step(review.run.run_id, 1)).state == "succeeded"

                help_result = await run(AgentPurpose.GUIDED_HELP, submission=first)
                assert help_result.run.state == RunState.SUCCEEDED
                assert help_result.result.evidence.submission_id == first
                assert help_result.result.requested_level == "location"
                assert help_result.result.delivered_level == "concept"
                assert help_result.result.gaps
                denied = await run(AgentPurpose.GUIDED_HELP, submission=foreign_id)
                assert (
                    denied.run.state == RunState.FAILED
                    and denied.failure_code == "evidence_unavailable"
                )

                focused = await run(AgentPurpose.FOCUSED_TRAINING)
                assert focused.run.state == RunState.SUCCEEDED
                (item,) = focused.result.items
                assert item.problem_version_id == problem_version and item.estimated_minutes == 20
                pending = await submit(owner, None)

                async def add_later_evidence():
                    await finish(pending, "accepted")
                    await submit(owner, "wrong_answer")

                periodic = await run(
                    AgentPurpose.PERIODIC_REVIEW, restart=True, after_enqueue=add_later_evidence
                )
                assert periodic.run.state == RunState.SUCCEEDED
                assert periodic.result.cycle_days == 21 and periodic.result.sessions_per_week == 2
                assert {item.submission_id for item in periodic.result.evidence} == {first, passed}
                assert periodic.result.items[0].problem_version_id == problem_version
                prior_pass = next(
                    item for item in periodic.result.evidence if item.submission_id == passed
                )
                assert periodic.result.items[0].timing.last_accepted_at == prior_pass.submitted_at
                assert periodic.result.items[0].timing.last_attempt_at == prior_pass.submitted_at
                interrupted = await storage.get_step(periodic.run.run_id, 2)
                assert interrupted.error_code == "learning_execution_interrupted"
                resumed = await storage.get_step(periodic.run.run_id, 3)
                assert (
                    resumed.state == "succeeded"
                    and resumed.invocation_version == "training_candidates-v1"
                )

                trace = await learning.trace(owner, periodic.run.run_id)
                assert trace.attempts == 2 and trace.tool_calls == trace.finished_tool_calls == 2
                assert [row.state for row in trace.steps] == ["succeeded", "failed", "succeeded"]

                with pytest.raises(AgentRecordUnavailable):
                    await learning.save_plan(foreign, periodic.run.run_id)
                plans = await asyncio.gather(
                    *(learning.save_plan(owner, periodic.run.run_id) for _ in range(3))
                )
                assert {plan.id for plan in plans} == {plans[0].id}
                assert plans[0].item_count == 1
                assert plans[0].items[0].problem_version_id == problem_version
                assert {item.submission_id for item in plans[0].items[0].evidence} == {
                    first,
                    passed,
                }

                pending_help = await submit(owner, None)

                async def finish_after_help_was_queued():
                    await finish(pending_help, "wrong_answer")

                earlier_help = await run(
                    AgentPurpose.GUIDED_HELP,
                    submission=pending_help,
                    after_enqueue=finish_after_help_was_queued,
                )
                assert earlier_help.run.state == RunState.SUCCEEDED
                assert earlier_help.result.evidence.submission_id == pending_help
                assert earlier_help.result.evidence.state == "unobserved"
                assert earlier_help.result.evidence.verdict is None
                assert earlier_help.result.delivered_level == "concept"

                async with sessions.begin() as db:
                    await db.execute(text("UPDATE problems SET enabled=false"))
                with pytest.raises(LearningUnavailable, match="recommendation changed"):
                    await learning.save_plan(owner, focused.run.run_id)
                replayed = await learning.save_plan(owner, periodic.run.run_id)
                assert replayed.id == plans[0].id and not replayed.items[0].available

                skill = learning.catalog.list_versions(AgentPurpose.PERIODIC_REVIEW)[0]
                session = await learning.create_session(owner, AgentPurpose.PERIODIC_REVIEW)
                revoked = await learning.prepare(
                    owner, session.session_id, uuid4(), skill.version_id, AgentLearningInput()
                )
                await identity.logout(login_owner.token, login_owner.csrf_token)
                assert await worker.run_once()
                async with admin.connect() as db:
                    failure = await db.scalar(
                        text(
                            "SELECT failure_code FROM learning_agent_workflow_data "
                            "WHERE run_id=:run"
                        ),
                        {"run": revoked.run.run_id},
                    )
                assert failure == "authorization_unavailable"

        finally:
            await worker_engine.dispose()
            await admin.dispose()

    asyncio.run(exercise())
