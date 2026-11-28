import asyncio
import secrets
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from conftest import connect
from test_diagnostic_batch_replay import batch as batch_fixture
from test_submission_create import setup_app

from kf_oj.contracts.diagnostic_batch import ConfirmBatchDiagnostic
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.contests.public import AgentUnavailableDuringContest
from kf_oj.modules.identity.public import (
    AcceptedSession,
    AuthenticationFailed,
    accepted_session_access,
)
from kf_oj.modules.learning.diagnostic_inputs import (
    DiagnosticInputsUnavailable,
    resolve_public_sample_diagnostic_inputs,
)
from kf_oj.workers import diagnostic_batch_replay


def test_diagnostic_inputs_require_explicit_live_owner_and_public_version(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            identity = app.state.identity

            async def login(username):
                password = secrets.token_urlsafe(24)
                await identity.create_account(username, password)
                return await identity.login(username, password, peer="127.0.0.1")

            owner = await login("diagnostic_owner")
            outsider = await login("diagnostic_other")
            version = await app.state.problems.import_directory(tmp_path / "import")
            source = "int main(void) { return 0; }\n"
            submission = await app.state.submissions.create(
                owner.principal,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    source=source,
                    idempotency_key=uuid4(),
                ),
            )

            async def resolve(
                actor=owner.principal, *, confirmed=True, version_id=version, index=0
            ):
                async with app.state.sessions.begin() as session:
                    return await resolve_public_sample_diagnostic_inputs(
                        session,
                        identity,
                        actor,
                        submission_id=submission.submission_id,
                        problem_version_id=version_id,
                        sample_ordinal=index,
                        source_execution_confirmed=confirmed,
                    )

            with pytest.raises(DiagnosticInputsUnavailable, match="confirmation"):
                await resolve(confirmed=False)
            for actor, version_id, index in (
                (outsider.principal, version, 0),
                (owner.principal, uuid4(), 0),
                (owner.principal, version, 1),
            ):
                with pytest.raises(DiagnosticInputsUnavailable, match="input unavailable"):
                    await resolve(actor, version_id=version_id, index=index)

            plan = await resolve()
            assert (plan.account_id, plan.source.submission_id) == (
                owner.principal.account_id,
                submission.submission_id,
            )
            assert plan.source.problem_version_id == plan.sample.version_id == version
            assert plan.source.language == "c17"
            assert plan.source.artifact.digest == sha256(source.encode()).hexdigest()
            assert plan.source.artifact.bytes == len(source.encode())
            assert (plan.sample.input_bytes, plan.sample.expected_bytes) == (b"1\n", b"2\n")

            prepared = batch_fixture(boundary=True)
            prepared = replace(
                prepared,
                manifest=prepared.manifest.model_copy(update={"problem_version_id": version}),
            )
            confirmation = ConfirmBatchDiagnostic(
                submission_id=submission.submission_id,
                problem_version_id=version,
                manifest_sha256=prepared.manifest_sha256,
                source_execution_confirmed=True,
            )
            count = 0
            revoke_during_execution = False

            async def run_batch(batch, candidate, worker):
                nonlocal count
                assert batch == prepared and candidate == source.encode()
                count += 1
                if revoke_during_execution:
                    await identity.logout(owner.token, owner.csrf_token)
                return batch.receipt() | {
                    "state": "boundary_observed",
                    "measurements": [
                        {"outcome": "time_limit", "cpu_time_us": 123, "memory_peak_bytes": 456}
                    ],
                    "sampling": {"kernel": "internal-host"},
                }

            monkeypatch.setattr(diagnostic_batch_replay, "replay_batch", run_batch)

            async def replay(actor=owner.principal, *, update=None, authority=identity):
                actual = confirmation.model_copy(update=update) if update else confirmation
                return await diagnostic_batch_replay.replay_confirmed_batch(
                    app.state.sessions,
                    authority,
                    app.state.problems,
                    app.state.problems.artifacts,
                    actor,
                    actual,
                    prepared,
                    None,
                )

            with pytest.raises(ValueError, match="explicit confirmation"):
                ConfirmBatchDiagnostic.model_validate(
                    confirmation.model_dump() | {"source_execution_confirmed": 1}
                )
            for actor, update in (
                (outsider.principal, None),
                (owner.principal, {"problem_version_id": uuid4()}),
                (owner.principal, {"manifest_sha256": "f" * 64}),
                (owner.principal, {"source_execution_confirmed": False}),
            ):
                with pytest.raises(DiagnosticInputsUnavailable):
                    await replay(actor, update=update)
            assert count == 0
            worker_authority = accepted_session_access(
                owner.principal.account_id, AcceptedSession.from_principal(owner.principal)
            )
            worker_actor = replace(owner.principal, session_token_hash=None)
            assert (await replay(worker_actor, authority=worker_authority)).model_dump() == {
                "schema_version": 1,
                "submission_id": submission.submission_id,
                "problem_version_id": version,
                "purpose": "boundary",
                "outcome": "resource_limit",
            }

            operator = await connect(postgres_url)
            try:
                await operator.execute("UPDATE problems SET enabled=false")
                with pytest.raises(DiagnosticInputsUnavailable):
                    await replay()
                assert count == 1
                await operator.execute("UPDATE problems SET enabled=true")
                await operator.execute(
                    "INSERT INTO scheduled_contests VALUES "
                    "('ar08-policy','AR-08 policy',"
                    "now()-interval '1 minute',now()+interval '1 hour')"
                )
                await operator.execute(
                    "INSERT INTO contest_entrants VALUES ($1,'ar08-policy')",
                    owner.principal.account_id,
                )
                with pytest.raises(AgentUnavailableDuringContest):
                    await resolve()
                with pytest.raises(AgentUnavailableDuringContest):
                    await replay()
                assert count == 1
                await operator.execute(
                    "DELETE FROM contest_entrants WHERE account_id=$1",
                    owner.principal.account_id,
                )
            finally:
                await operator.close()

            revoke_during_execution = True
            with pytest.raises(AuthenticationFailed):
                await replay(worker_actor, authority=worker_authority)
            assert count == 2
            with pytest.raises(AuthenticationFailed):
                await resolve()

    asyncio.run(exercise())
