"""Run the tool gate against real skills, owned data, and shared PostgreSQL quota."""

import asyncio
import json
import secrets
from uuid import uuid4

import pytest
from test_submission_create import setup_app

from kf_oj.contracts.agent_runtime import AgentPurpose, RunState
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.public import (
    AGENT_TOOL_SCOPES,
    SUBMISSION_HISTORY,
    TRAINING_CANDIDATES,
    AgentToolLimits,
    agent_storage,
    scoped_agent_tools,
)
from kf_oj.modules.skills.public import (
    BuiltinSkills,
    SkillBindings,
    SkillBindingUnavailable,
    load_builtin_skills,
    parse_builtin_skill,
)
from kf_oj.runtime.tools import (
    ToolArgumentsInvalid,
    ToolBudgetExceeded,
    ToolContext,
    ToolDenied,
    ToolKey,
)


def test_real_tool_isolation_and_shared_budget(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            identity = app.state.identity
            sessions = app.state.sessions
            purpose = AgentPurpose.FOCUSED_TRAINING
            original = load_builtin_skills().list_versions(purpose)[0]
            old_id = original.version_id
            history = SUBMISSION_HISTORY
            candidates = TRAINING_CANDIDATES
            only_history = frozenset({history})
            both = frozenset({history, candidates})
            document = original.declaration.model_dump(mode="json")
            next_version = parse_builtin_skill(json.dumps(document | {"release": 2}).encode())
            new_id = next_version.version_id
            skills = SkillBindings(
                sessions, identity, BuiltinSkills((original, next_version)), AGENT_TOOL_SCOPES
            )
            problem = await app.state.problems.import_directory(tmp_path / "import")

            async def actor(username):
                password = secrets.token_urlsafe(24)
                await identity.create_account(username, password)
                login = await identity.login(username, password, peer="tool-integration")
                submitted = await app.state.submissions.create(
                    login.principal,
                    CreateSubmission(
                        problem_version_id=problem,
                        language="c17",
                        idempotency_key=uuid4(),
                        source="synthetic; never executed",
                    ),
                )
                return login, submitted.submission_id

            async def start(principal, configuration):
                storage = agent_storage(sessions, principal)
                session = await storage.create_session(purpose)
                run = await storage.enqueue(
                    session.session_id,
                    request_id=uuid4(),
                    skill_version_id=configuration.version_id,
                    configuration_digest=configuration.digest,
                    input_digest="b" * 64,
                )
                run = await storage.transition(run.run_id, run.revision, RunState.RUNNING)
                return ToolContext(run.run_id, run.revision)

            owner, own_id = await actor("tool_owner")
            other, other_id = await actor("tool_other")
            await skills.configure(
                owner.principal,
                old_id,
                parameters=b'{"daily_minutes":20}',
                enabled=True,
                authorized_tools=only_history,
            )
            frozen = await skills.freeze(owner.principal, old_id)
            first = await start(owner.principal, frozen)
            with pytest.raises(SkillBindingUnavailable):
                await skills.binding(other.principal, original.declaration.slug)
            with pytest.raises(SkillBindingUnavailable):
                await skills.configuration(other.principal, old_id, frozen.digest)
            with pytest.raises(ValueError, match="approved scope"):
                await skills.configure(
                    other.principal,
                    old_id,
                    parameters=b"{}",
                    enabled=True,
                    authorized_tools=frozenset({ToolKey("unknown", 1)}),
                )

            # Upgrade preferences without replacing the old run's version or granting new tools.
            await skills.configure(
                owner.principal,
                new_id,
                parameters=b'{"daily_minutes":40}',
                enabled=True,
                authorized_tools=both,
            )
            upgraded = await skills.freeze(owner.principal, new_id)
            assert upgraded.parameters.daily_minutes == 40
            assert (await skills.configuration(owner.principal, old_id, frozen.digest)) == frozen
            assert frozen.parameters.daily_minutes == 20
            with pytest.raises(SkillBindingUnavailable):
                await skills.freeze(owner.principal, old_id)
            second = await start(owner.principal, upgraded)
            third = await start(owner.principal, upgraded)
            await skills.configure(
                other.principal, old_id, parameters=b"{}", enabled=True, authorized_tools=both
            )
            other_config = await skills.freeze(other.principal, old_id)
            outsider = await start(other.principal, other_config)
            parallel_run = await start(other.principal, other_config)

            limits = AgentToolLimits(
                run_attempts=1, account_attempts_per_hour=2, service_attempts_per_hour=3
            )
            a = scoped_agent_tools(sessions, owner.principal, identity, skills, limits)
            b = scoped_agent_tools(sessions, other.principal, identity, skills, limits)
            with pytest.raises(ToolArgumentsInvalid):
                await a.invoke(
                    first,
                    history,
                    json.dumps({"owner_id": str(other.principal.account_id)}).encode(),
                )
            with pytest.raises(ToolDenied):
                await b.invoke(first, history, b"{}")
            with pytest.raises(ToolDenied):
                await a.invoke(first, candidates, b"{}")
            rows = (await a.invoke(first, history, b"{}")).records
            assert tuple(r.submission_id for r in rows) == (own_id,)
            result = await a.invoke(second, candidates, b'{"count":1}')
            assert tuple(item.problem_version_id for item in result.items) == (problem,)
            with pytest.raises(ToolBudgetExceeded, match="run tool attempts"):
                await a.invoke(first, history, b"{}")
            with pytest.raises(ToolBudgetExceeded, match="shared budget"):
                await a.invoke(third, history, b"{}")

            parallel = scoped_agent_tools(sessions, other.principal, identity, skills, limits)
            results = await asyncio.gather(
                b.invoke(outsider, history, b"{}"),
                parallel.invoke(parallel_run, history, b"{}"),
                return_exceptions=True,
            )
            blocked = [r for r in results if isinstance(r, ToolBudgetExceeded)]
            assert len(blocked) == 1 and str(blocked[0]) == "Agent tool shared budget exhausted"
            granted = [r for r in results if not isinstance(r, Exception)]
            assert len(granted) == 1
            assert tuple(r.submission_id for r in granted[0].records) == (other_id,)

            await skills.configure(
                owner.principal,
                new_id,
                parameters=b'{"daily_minutes":40}',
                enabled=False,
                authorized_tools=both,
            )
            with pytest.raises(ToolDenied):
                await a.invoke(third, history, b"{}")
            with pytest.raises(SkillBindingUnavailable):
                await skills.freeze(owner.principal, new_id)
            await skills.configure(
                owner.principal,
                new_id,
                parameters=b'{"daily_minutes":40}',
                enabled=True,
                authorized_tools=both,
            )
            assert (await skills.freeze(owner.principal, new_id)).digest != upgraded.digest
            with pytest.raises(ToolDenied):
                await a.invoke(third, history, b"{}")
            await identity.logout(other.token, other.csrf_token)
            with pytest.raises(ToolDenied):
                await b.invoke(outsider, history, b"{}")

    asyncio.run(exercise())
