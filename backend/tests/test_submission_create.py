import asyncio
import json
import secrets
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_problems import import_fixture, platform_role

from kf_oj.api.app import create_app
from kf_oj.contracts.judging import MAX_SOURCE_BYTES, Language
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.messaging.models import Outbox
from kf_oj.modules.submissions.models import Evaluation, Submission
from kf_oj.modules.submissions.public import IdempotencyConflict, SubmissionUnavailable


def test_submission_input_utf8_and_unknown_fields():
    base = dict(problem_version_id=str(uuid4()), language="c17", idempotency_key=str(uuid4()))
    CreateSubmission(**base, source="é" * (MAX_SOURCE_BYTES // 2))
    for extra in [
        dict(source="é" * (MAX_SOURCE_BYTES // 2 + 1)),
        dict(source="x", argv=[]),
        dict(source="x", user_id=str(uuid4())),
        dict(source=""),
        dict(source="\ud800"),
    ]:
        with pytest.raises(ValueError):
            CreateSubmission(**base, **extra)


async def setup_app(postgres_url, tmp_path):
    _, policy = import_fixture(tmp_path / "import")
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=postgres_url,
            artifact_root=tmp_path / "artifacts",
            judge_admission=policy,
        )
    )
    engine = create_async_engine(postgres_url)
    platform_role(engine)
    # Test creates separate permission-constrained platform connections used by application.
    app_engine_build = __import__("kf_oj.api.app", fromlist=["build_engine"])
    return app, engine, app_engine_build


def test_actual_concurrent_idempotency_version_and_outbox(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            account = await app.state.identity.create_account(
                "sub_fixture", secrets.token_urlsafe(24)
            )
            version = await app.state.problems.import_directory(tmp_path / "import")
            request = CreateSubmission(
                problem_version_id=version,
                language="c17",
                idempotency_key=uuid4(),
                source="synthetic source, never executed",
            )
            results = await asyncio.gather(
                *(app.state.submissions.create(account, request) for _ in range(8))
            )
            assert len({r.submission_id for r in results}) == 1
            async with app.state.sessions() as s:
                for model in [Submission, Evaluation, Outbox]:
                    assert await s.scalar(select(func.count()).select_from(model)) == 1
                stored = await s.get(Submission, results[0].submission_id)
                data = await app.state.problems.artifacts.read(
                    s, app.state.problems.artifacts.store.reference(request.source.encode())
                )
                assert data == request.source.encode() and stored.language == "c17"
            with pytest.raises(IdempotencyConflict):
                await app.state.submissions.create(
                    account, request.model_copy(update={"source": "different source"})
                )
            admin = create_async_engine(postgres_url)
            async with admin.begin() as c:
                await c.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                await c.execute(
                    text("UPDATE evaluations SET state='finished',verdict='accepted' WHERE id=:id"),
                    {"id": results[0].evaluation_id},
                )
            await admin.dispose()
            assert (await app.state.submissions.create(account, request)).state == "finished"
            cpp = await app.state.submissions.create(
                account,
                request.model_copy(update={"language": Language.CPP20, "idempotency_key": uuid4()}),
            )
            assert cpp.submission_id != results[0].submission_id
            async with app.state.sessions.begin() as s:
                await s.execute(text("UPDATE problems SET enabled=false"))
            assert (await app.state.submissions.create(account, request)).submission_id == results[
                0
            ].submission_id
            with pytest.raises(SubmissionUnavailable):
                await app.state.submissions.create(
                    account, request.model_copy(update={"idempotency_key": uuid4()})
                )

    asyncio.run(run())


def test_actual_database_failure_rolls_back_entire_creation(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            account = await app.state.identity.create_account(
                "rollback_fixture", secrets.token_urlsafe(24)
            )
            version = await app.state.problems.import_directory(tmp_path / "import")
            admin = create_async_engine(postgres_url)
            async with admin.begin() as c:
                await c.execute(
                    text(
                        """CREATE FUNCTION test_reject_outbox() RETURNS trigger
                        LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'synthetic rollback'; END $$"""
                    )
                )
                await c.execute(
                    text(
                        "CREATE TRIGGER test_reject_outbox BEFORE INSERT ON outbox "
                        "FOR EACH ROW EXECUTE FUNCTION test_reject_outbox()"
                    )
                )
            request = CreateSubmission(
                problem_version_id=version,
                language="c17",
                idempotency_key=uuid4(),
                source="rollback synthetic bytes",
            )
            with pytest.raises(DBAPIError):
                await app.state.submissions.create(account, request)
            async with app.state.sessions() as s:
                for model in [Submission, Evaluation, Outbox]:
                    assert await s.scalar(select(func.count()).select_from(model)) == 0
                ref = app.state.problems.artifacts.store.reference(request.source.encode())
                assert await s.get(Artifact, ref.digest) is None
                # Bytes can survive rollback as a collectable orphan; no durable reference exists.
                assert app.state.problems.artifacts.store.read(ref) == request.source.encode()
            async with admin.begin() as c:
                await c.execute(text("DROP TRIGGER test_reject_outbox ON outbox"))
                await c.execute(text("DROP FUNCTION test_reject_outbox()"))
            assert (await app.state.submissions.create(account, request)).state == "queued"
            await admin.dispose()

    asyncio.run(run())


def test_actual_authenticated_creation_request_boundaries(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            password = secrets.token_urlsafe(24)
            await app.state.identity.create_account("api_submission", password)
            version = await app.state.problems.import_directory(tmp_path / "import")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client:
                req = dict(
                    problem_version_id=str(version),
                    language="c17",
                    idempotency_key=str(uuid4()),
                    source="synthetic ordinary data",
                )
                assert (await client.post("/api/v1/submissions", json=req)).status_code == 403
                login = await client.post(
                    "/api/v1/auth/login",
                    json=dict(username="api_submission", password=password),
                    headers={"Origin": "http://localhost:8000"},
                )
                assert login.status_code == 200
                headers = {
                    "Origin": "http://localhost:8000",
                    "X-CSRF-Token": login.json()["csrf_token"],
                }
                assert (
                    await client.post("/api/v1/submissions", json=req, headers=headers)
                ).status_code == 201
                assert (
                    await client.post(
                        "/api/v1/submissions", json=req | {"user_id": str(uuid4())}, headers=headers
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        "/api/v1/submissions", json=req | {"language": "python"}, headers=headers
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        "/api/v1/submissions", json=req | {"source": "changed"}, headers=headers
                    )
                ).status_code == 409
                assert (
                    await client.post(
                        "/api/v1/submissions",
                        content=b"x" * (6 * MAX_SOURCE_BYTES + 8193),
                        headers=headers | {"Content-Type": "application/json"},
                    )
                ).status_code == 413
                # JSON escaping can exceed old1MiB body ceiling while valid source remains256KiB.
                req["idempotency_key"] = str(uuid4())
                req["source"] = "\x01" * MAX_SOURCE_BYTES
                assert len(json.dumps(req)) > 1024 * 1024
                assert (
                    await client.post("/api/v1/submissions", json=req, headers=headers)
                ).status_code == 201

    asyncio.run(run())
