"""Real PostgreSQL storage/authorization with explicitly synthetic grading records.

No fixture source is executed and no synthetic result is a real judge acceptance.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.learning import CreateTraining, LearningParameters, UpdateTrainingNote
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.public import IdempotencyConflict, LearningUnavailable
from kf_oj.modules.learning.service import LearningService
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata
from kf_oj.modules.problems.models import ProblemVersion
from kf_oj.modules.submissions import public as records


def test_problem_note_is_private_and_persistent(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = create_async_engine(postgres_url)
        origin = "http://localhost:5173"

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=origin, headers={"Origin": origin}
            )

        async def login(browser, username):
            password = secrets.token_urlsafe(24)
            await app.state.identity.create_account(username, password)
            response = await browser.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]

        try:
            async with app.router.lifespan_context(app), client() as owner, client() as other:
                version = await app.state.problems.import_directory(tmp_path / "import")
                path = f"/api/v1/learning/problem-notes/{version}"
                assert (await owner.get(path)).status_code == 401
                await login(owner, "problem_note_owner")
                await login(other, "problem_note_other")
                assert (await owner.get(path)).json() == {
                    "problem_version_id": str(version),
                    "note": "",
                    "updated_at": None,
                }
                draft = {"note": "## 边界\n\n注意 $l < r$。\n\n```cpp\nint l = 0;\n```"}
                denied = await owner.put(path, json=draft, headers={"x-csrf-token": ""})
                assert denied.status_code == 403
                assert (await owner.put(path, json={"note": "字" * 1001})).status_code == 422
                saved = await owner.put(path, json=draft)
                assert saved.status_code == 200 and saved.json()["note"] == draft["note"]
                assert saved.json()["updated_at"]
                assert (await owner.get(path)).json() == saved.json()
                assert (await other.get(path)).json()["note"] == ""
                own_other = await other.put(path, json={"note": "另一份笔记"})
                assert own_other.status_code == 200
                assert (await owner.get(path)).json() == saved.json()
                cleared = await owner.put(path, json={"note": ""})
                assert cleared.status_code == 200 and cleared.json()["updated_at"] is None
                assert (await owner.get(path)).json()["note"] == ""
                assert (await other.get(path)).json() == own_other.json()
                missing = "/api/v1/learning/problem-notes/" + str(uuid4())
                assert (await owner.put(missing, json=draft)).status_code == 404
                async with operator.connect() as db:
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "'kf_oj_learning_worker', 'learning_problem_notes', 'SELECT')"
                        )
                    )
        finally:
            await operator.dispose()

    asyncio.run(run())


def test_real_plan_storage_scope_history_and_progress(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        admin = create_async_engine(postgres_url)
        try:
            async with app.router.lifespan_context(app):
                a = await app.state.identity.create_account("learning_a", secrets.token_urlsafe(24))
                b = await app.state.identity.create_account("learning_b", secrets.token_urlsafe(24))
                old = await app.state.problems.import_directory(tmp_path / "import")

                async def submit(owner, version, verdict="accepted", days=0):
                    response = await app.state.submissions.create(
                        owner,
                        CreateSubmission(
                            problem_version_id=version,
                            language="c17",
                            idempotency_key=uuid4(),
                            source="synthetic learning storage fixture; never executed",
                        ),
                    )
                    async with admin.begin() as c:
                        await c.execute(
                            text("UPDATE evaluations SET state='finished',verdict=:v WHERE id=:id"),
                            {"v": verdict, "id": response.evaluation_id},
                        )
                        if days:
                            await c.execute(
                                text(
                                    "UPDATE submissions SET created_at=clock_timestamp()"
                                    "-:days * interval '1 day' WHERE id=:id"
                                ),
                                {"days": days, "id": response.submission_id},
                            )
                    return response

                await submit(a, old, days=120)
                version = await app.state.problems.import_directory(tmp_path / "import")
                async with app.state.sessions.begin() as s:
                    original = await s.get(ProblemVersion, version)
                    before = {
                        col.name: getattr(original, col.name)
                        for col in ProblemVersion.__table__.columns
                    }
                    s.add(
                        ProblemLearningMetadata(
                            problem_version_id=version, tags=["排序"], difficulty="easy", revision=1
                        )
                    )
                recommended = await app.state.learning.recommend(a, LearningParameters())
                assert recommended.items[0].problem_version_id == version
                assert not recommended.items[0].previously_accepted
                body = CreateTraining(idempotency_key=uuid4(), count=1)
                plans = await asyncio.gather(
                    *(app.state.learning.create(a, body) for _ in range(3))
                )
                plan = plans[0]
                assert len({p.id for p in plans}) == 1
                assert plan.items[0].progress == "not_started"
                with pytest.raises(IdempotencyConflict):
                    await app.state.learning.create(a, body.model_copy(update={"title": "changed"}))
                assert (await app.state.learning.page(b)).items == ()
                with pytest.raises(LearningUnavailable):
                    await app.state.learning.detail(b, plan.id)
                with pytest.raises(LearningUnavailable):
                    await app.state.learning.note(
                        b, plan.id, plan.items[0].id, UpdateTrainingNote(note="foreign")
                    )
                # Other account, earlier own AC, and older version do not complete this plan.
                await submit(b, version)
                await submit(a, version, days=1)
                await submit(a, old)
                assert (await app.state.learning.detail(a, plan.id)).completed_count == 0
                await submit(a, version, "wrong_answer")
                assert (await app.state.learning.detail(a, plan.id)).items[
                    0
                ].progress == "attempted"
                ac = await submit(a, version)
                await submit(a, version, "wrong_answer")
                assert (await app.state.learning.detail(a, plan.id)).completed_count == 1
                updated = await app.state.learning.note(
                    a, plan.id, plan.items[0].id, UpdateTrainingNote(note="保留复习笔记")
                )
                assert updated.items[0].note == "保留复习笔记"
                async with app.state.sessions.begin() as s:
                    metadata = await s.get(ProblemLearningMetadata, version)
                    metadata.tags, metadata.difficulty, metadata.revision = ["图"], "hard", 2
                async with app.state.sessions() as s:
                    after = await s.get(ProblemVersion, version)
                    assert before == {
                        col.name: getattr(after, col.name)
                        for col in ProblemVersion.__table__.columns
                    }
                reread = await LearningService(app.state.sessions).detail(a, plan.id)
                assert reread.items[0].tags == ("排序",) and reread.items[0].metadata_revision == 1
                assert reread.items[0].note == "保留复习笔记"
                # Old plan + AC beyond overview window remain complete via separate query.
                async with admin.begin() as c:
                    await c.execute(
                        text(
                            "UPDATE learning_plans SET created_at=clock_timestamp()"
                            "-interval '150 days' WHERE id=:id"
                        ),
                        {"id": plan.id},
                    )
                    await c.execute(
                        text(
                            "UPDATE submissions SET created_at=clock_timestamp()"
                            "-interval '100 days' WHERE id=:id"
                        ),
                        {"id": ac.submission_id},
                    )
                    # 501 synthetic WA submissions truncate the overview; no judge attempts.
                    await c.execute(
                        text("""WITH inserted AS (
                        INSERT INTO submissions(id,account_id,problem_id,problem_version_id,
                            language,source_digest,idempotency_key,request_digest)
                        SELECT gen_random_uuid(),account_id,problem_id,problem_version_id,
                            language,source_digest,gen_random_uuid(),request_digest
                        FROM submissions CROSS JOIN generate_series(1,501) WHERE id=:sid
                        RETURNING id,problem_version_id
                    ) INSERT INTO evaluations(id,submission_id,problem_version_id,state,verdict)
                      SELECT gen_random_uuid(),id,problem_version_id,'finished','wrong_answer'
                      FROM inserted"""),
                        {"sid": ac.submission_id},
                    )
                overview = await app.state.learning.overview(a)
                assert overview.truncated and overview.submission_count == 500
                assert overview.accepted_version_ids == ()
                assert (
                    (await app.state.learning.recommend(a, LearningParameters()))
                    .items[0]
                    .previously_accepted
                )
                reread = await app.state.learning.detail(a, plan.id)
                assert reread.completed_count == 1
                assert any(
                    e.submission_id == ac.submission_id for e in reread.items[0].progress_evidence
                )
                async with app.state.sessions() as s:
                    progress = await records.learning_progress(
                        s, a, {version}, since=datetime.now(UTC) - timedelta(days=150)
                    )
                    assert progress[version].status == "completed"
                # A newer published version never silently replaces the stored plan reference.
                await app.state.problems.import_directory(tmp_path / "import")
                reread = await app.state.learning.detail(a, plan.id)
                assert reread.items[0].problem_version_id == version
                assert not reread.items[0].is_current_version
        finally:
            await admin.dispose()

    asyncio.run(run())
