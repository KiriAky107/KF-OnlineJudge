import asyncio
import hashlib
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import import_fixture, platform_role

from kf_oj.api.app import create_app
from kf_oj.contracts.judging import (
    JobManifest,
    LeaseBinding,
    manifest_bytes,
)
from kf_oj.core.settings import Settings
from kf_oj.modules.identity.service import token_digest
from kf_oj.modules.submissions.models import Evaluation, JudgeAttempt, Submission
from kf_oj.modules.submissions.public import SubmissionUnavailable
from kf_oj.modules.submissions.queries import SubmissionQueries


def test_canonical_manifest_snapshot():
    root = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts"
    manifest = JobManifest.model_validate_json((root / "judge-manifest.valid.json").read_bytes())
    assert manifest_bytes(manifest) == (root / "canonical-manifest.v1.txt").read_bytes()


async def install_attempt(
    sessions, app, account, version_id, *, source=b"synthetic source", language="c17"
):
    # Explicit controlled DB fixture, not a simulated Coordinator API or claimed execution.
    # E implements actual transactional claim; here the real B01 schema protects associations.
    submission_id, evaluation_id, attempt_id, worker_id = (uuid4() for _ in range(4))
    lease_token = secrets.token_hex(32)
    async with app.state.sessions.begin() as session:
        source_ref = await app.state.problems.artifacts.put(session, source)
        projected = await app.state.problems.for_submission(session, version_id)
        session.add(
            Submission(
                id=submission_id,
                account_id=account.account_id,
                problem_id=projected.problem_id,
                problem_version_id=version_id,
                language=language,
                source_digest=source_ref.digest,
                idempotency_key=uuid4(),
                request_digest="e" * 64,
            )
        )
        await session.flush()
        # Use column-level INSERT. ORM inserts optional execution fields, which the platform
        # deliberately cannot assign; application creation must use the same column projection.
        await session.execute(
            Evaluation.__table__.insert().values(
                id=evaluation_id, submission_id=submission_id, problem_version_id=version_id
            )
        )
    manifest = projected.manifest(evaluation_id, submission_id, language, source_ref)
    async with sessions.begin() as session:
        await session.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
        now = await session.scalar(select(func.now()))
        session.add(
            JudgeAttempt(
                id=attempt_id,
                evaluation_id=evaluation_id,
                worker_id=worker_id,
                number=1,
                fencing_version=1,
                lease_token_hash=token_digest(lease_token),
                expires_at=now + timedelta(minutes=10),
                manifest=manifest.model_dump(mode="json"),
                manifest_digest=hashlib.sha256(manifest_bytes(manifest)).hexdigest(),
            )
        )
        await session.flush()
        await session.execute(
            update(Evaluation)
            .where(Evaluation.id == evaluation_id)
            .values(
                current_attempt_id=attempt_id, state="claimed", attempts_count=1, fencing_version=1
            )
        )
    return (
        LeaseBinding(
            evaluation_id=evaluation_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            fencing_version=1,
        ),
        lease_token,
        manifest,
    )


def test_real_two_user_two_version_manifest_and_artifact_authorization(postgres_url, tmp_path):
    root = tmp_path / "import"
    _, policy = import_fixture(root)

    async def run():
        owner = create_async_engine(postgres_url)
        owner_sessions = async_sessionmaker(owner, expire_on_commit=False)
        app = create_app(
            Settings(
                _env_file=None,
                environment="test",
                database_url=postgres_url,
                artifact_root=tmp_path / "artifacts",
                judge_admission=policy,
            )
        )
        try:
            async with app.router.lifespan_context(app):
                platform_role(app.state.sessions.kw["bind"])
                password = secrets.token_hex(16)
                first_user = await app.state.identity.create_account("first_user", password)
                second_password = secrets.token_hex(16)
                second_user = await app.state.identity.create_account(
                    "second_user", second_password
                )
                old_version = await app.state.problems.import_directory(root)
                first, token, original = await install_attempt(
                    owner_sessions, app, first_user, old_version
                )
                (root / "expected-1").write_bytes(b"different hidden version\x00")
                new_version = await app.state.problems.import_directory(root)
                second, other_token, other_manifest = await install_attempt(
                    owner_sessions, app, second_user, new_version, source=b"other user's source"
                )
                queries = SubmissionQueries(app.state.sessions, app.state.problems.artifacts)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
                ) as client:
                    login = await client.post(
                        "/api/v1/auth/login",
                        headers={"Origin": "http://localhost:8000"},
                        json={"username": "first_user", "password": password},
                    )
                    assert login.status_code == 200
                    authenticated = await app.state.identity.authenticate(
                        client.cookies.get("kf_session")
                    )
                    assert (
                        await queries.owned_source(authenticated, original.submission_id)
                        == b"synthetic source"
                    )
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
                    ) as other_client:
                        other_login = await other_client.post(
                            "/api/v1/auth/login",
                            headers={"Origin": "http://localhost:8000"},
                            json={"username": "second_user", "password": second_password},
                        )
                        assert other_login.status_code == 200
                        other_principal = await app.state.identity.authenticate(
                            other_client.cookies.get("kf_session")
                        )
                        assert other_principal.account_id == second_user.account_id
                        with pytest.raises(SubmissionUnavailable):
                            await queries.owned_source(other_principal, original.submission_id)
                    path = (
                        f"/api/v1/internal/evaluations/{first.evaluation_id}"
                        f"/attempts/{first.attempt_id}"
                    )
                    headers = {
                        "Authorization": "Bearer " + token,
                        "X-Worker-ID": str(first.worker_id),
                        "X-Fencing-Version": "1",
                    }
                    assert (
                        await client.get(path + "/manifest", headers=headers)
                    ).content == manifest_bytes(original)
                    # Changing the active problem version did not change an existing evaluation.
                    async with owner_sessions() as s:
                        evaluation = await s.get(Evaluation, first.evaluation_id)
                        assert evaluation.problem_version_id == old_version
                    assert (
                        await client.get(
                            path + "/manifest",
                            headers={k: v for k, v in headers.items() if k != "Authorization"},
                        )
                    ).status_code == 401
                    assert (
                        await client.get(
                            path + "/manifest",
                            headers=headers | {"Authorization": "Bearer " + other_token},
                        )
                    ).status_code == 404
                    assert (
                        await client.get(
                            path + "/manifest", headers=headers | {"X-Worker-ID": str(uuid4())}
                        )
                    ).status_code == 404
                    assert (
                        await client.get(
                            path + "/manifest", headers=headers | {"X-Fencing-Version": "2"}
                        )
                    ).status_code == 404
                    for reference in (
                        original.source,
                        original.cases[0].input,
                        original.cases[0].expected,
                    ):
                        response = await client.get(
                            path + "/artifacts/" + reference.digest, headers=headers
                        )
                        assert response.status_code == 200
                        assert len(response.content) == reference.bytes
                        assert hashlib.sha256(response.content).hexdigest() == reference.digest
                        assert response.headers["cache-control"] == "no-store"
                    assert (
                        await client.get(
                            path + "/artifacts/" + other_manifest.source.digest, headers=headers
                        )
                    ).status_code == 404
                    assert (
                        await client.get(
                            path + "/artifacts/" + other_manifest.cases[0].expected.digest,
                            headers=headers,
                        )
                    ).status_code == 404
                    assert (
                        await client.get(path + "/artifacts/" + "f" * 64, headers=headers)
                    ).status_code == 404
                    second_path = (
                        f"/api/v1/internal/evaluations/{second.evaluation_id}"
                        f"/attempts/{second.attempt_id}"
                    )
                    assert (
                        await client.get(second_path + "/manifest", headers=headers)
                    ).status_code == 404
                    assert (
                        await client.get(
                            path + "/manifest",
                            headers={
                                "Authorization": "Bearer " + client.cookies.get("kf_session"),
                                "X-Worker-ID": str(first.worker_id),
                                "X-Fencing-Version": "1",
                            },
                        )
                    ).status_code == 404
        finally:
            await owner.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid", ["expired", "revoked", "cancel_requested", "terminal", "old_attempt"]
)
def test_expired_revoked_cancelled_terminal_or_old_attempt_rejected(
    postgres_url, tmp_path, invalid
):
    root = tmp_path / "import"
    _, policy = import_fixture(root)

    async def run():
        owner = create_async_engine(postgres_url)
        sessions = async_sessionmaker(owner, expire_on_commit=False)
        app = create_app(
            Settings(
                _env_file=None,
                environment="test",
                database_url=postgres_url,
                artifact_root=tmp_path / "artifacts",
                judge_admission=policy,
            )
        )
        try:
            async with app.router.lifespan_context(app):
                platform_role(app.state.sessions.kw["bind"])
                account = await app.state.identity.create_account(
                    "fixture_user", secrets.token_hex(16)
                )
                version = await app.state.problems.import_directory(root)
                binding, token, manifest = await install_attempt(sessions, app, account, version)
                async with sessions.begin() as s:
                    await s.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                    if invalid == "expired":
                        await s.execute(
                            update(JudgeAttempt).values(
                                expires_at=func.now() - timedelta(seconds=1)
                            )
                        )
                    elif invalid == "revoked":
                        await s.execute(update(JudgeAttempt).values(revoked_at=func.now()))
                    elif invalid == "cancel_requested":
                        await s.execute(update(Evaluation).values(cancel_requested_at=func.now()))
                    elif invalid == "terminal":
                        await s.execute(update(Evaluation).values(state="finished"))
                    else:
                        new_attempt = uuid4()
                        now = await s.scalar(select(func.now()))
                        s.add(
                            JudgeAttempt(
                                id=new_attempt,
                                evaluation_id=binding.evaluation_id,
                                worker_id=uuid4(),
                                number=2,
                                fencing_version=2,
                                lease_token_hash="e" * 64,
                                expires_at=now + timedelta(minutes=1),
                                manifest=manifest.model_dump(mode="json"),
                                manifest_digest=hashlib.sha256(
                                    manifest_bytes(manifest)
                                ).hexdigest(),
                            )
                        )
                        await s.flush()
                        await s.execute(
                            update(Evaluation).values(
                                current_attempt_id=new_attempt, fencing_version=2, attempts_count=2
                            )
                        )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    prefix = (
                        f"/api/v1/internal/evaluations/{binding.evaluation_id}"
                        f"/attempts/{binding.attempt_id}"
                    )
                    headers = {
                        "Authorization": "Bearer " + token,
                        "X-Worker-ID": str(binding.worker_id),
                        "X-Fencing-Version": "1",
                    }
                    assert (
                        await client.get(prefix + "/manifest", headers=headers)
                    ).status_code == 404
                    assert (
                        await client.get(
                            prefix + "/artifacts/" + manifest.source.digest, headers=headers
                        )
                    ).status_code == 404
        finally:
            await owner.dispose()

    asyncio.run(run())
