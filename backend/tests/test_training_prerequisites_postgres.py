"""Verify owner-scoped prerequisite evidence with synthetic grading records."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_maintenance_authority import set_administrator
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1"
AGENT = f"{ROOT}/learning/agent"
ORIGIN = "http://localhost:5173"


def test_prerequisites_gate_recommendations_and_anchored_plan_saves(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        operator = await connect(postgres_url)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        async def login(browser, name, *, admin=False):
            password = secrets.token_urlsafe(24)
            principal = await app.state.identity.create_account(name, password)
            if admin:
                await set_administrator(operator, principal.account_id, True)
            response = await browser.post(
                f"{ROOT}/auth/login", json={"username": name, "password": password}
            )
            assert response.status_code == 200, response.text
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
            return principal

        async def grade(principal, version, verdict):
            submission = await app.state.submissions.create(
                principal,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    idempotency_key=uuid4(),
                    source="synthetic prerequisite evidence, never executed",
                ),
            )
            async with operator.transaction():
                await operator.execute(
                    "UPDATE submissions SET created_at=clock_timestamp() - interval '120 days' "
                    "WHERE id=$1",
                    submission.submission_id,
                )
            if verdict is not None:
                await finish(submission.evaluation_id, verdict)
            return submission

        async def finish(evaluation_id, verdict):
            async with operator.transaction():
                await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                await operator.execute(
                    "UPDATE evaluations SET state='finished', verdict=$1, "
                    "updated_at=clock_timestamp() WHERE id=$2",
                    verdict,
                    evaluation_id,
                )

        async def recommend(browser):
            response = await browser.post(
                f"{ROOT}/learning/recommendations", json={"tags": ["背包"], "count": 2}
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def start(browser, skill):
            created = await browser.post(f"{AGENT}/sessions", json={"purpose": "focused_training"})
            session = created.json()["session_id"]
            queued = await browser.post(
                f"{AGENT}/sessions/{session}/runs",
                json={"request_id": str(uuid4()), "skill_version_id": skill},
            )
            assert queued.status_code == 202, queued.text
            return f"{AGENT}/runs/{queued.json()['run_id']}"

        try:
            async with (
                app.router.lifespan_context(app),
                client() as administrator,
                client() as learner,
                client() as other,
            ):
                await login(administrator, "prerequisite_admin", admin=True)
                owner = await login(learner, "prerequisite_learner")
                outsider = await login(other, "prerequisite_other")
                foundation = await app.state.problems.import_directory(tmp_path / "import")
                destination = tmp_path / "target"
                import_fixture(destination, slug="prerequisite-target")
                target = await app.state.problems.import_directory(destination)
                admin = f"{ROOT}/admin/problem-versions"
                set_base = await administrator.put(
                    f"{admin}/{foundation}/learning-metadata",
                    json={"tags": ["动态规划", "数组"], "difficulty": "easy"},
                )
                assert set_base.status_code == 200 and set_base.json()["prerequisite_tags"] == []
                target_url = f"{admin}/{target}/learning-metadata"
                body = {"tags": ["背包"], "difficulty": "medium", "prerequisite_tags": ["动态规划"]}
                denied = await learner.put(target_url, json=body)
                assert denied.status_code == 403
                made = await administrator.put(target_url, json=body)
                assert made.status_code == 200, made.text
                assert made.json()["prerequisite_tags"] == ["动态规划"]
                legacy = await administrator.put(
                    target_url, json={"tags": ["背包"], "difficulty": "hard"}
                )
                assert legacy.json()["prerequisite_tags"] == ["动态规划"]
                assert legacy.json()["revision"] == made.json()["revision"] + 1

                await grade(outsider, foundation, "accepted")
                await grade(owner, foundation, "wrong_answer")
                cold = await recommend(learner)
                assert cold["items"] == []
                assert cold["prerequisite_gap"] == {
                    "missing_tags": ["动态规划"],
                    "affected_candidates": 1,
                    "remaining_tags": 0,
                }
                catalog = (await learner.get(f"{AGENT}/skills")).json()["versions"]
                focused = next(x for x in catalog if x["purpose"] == "focused_training")
                skill = focused["version_id"]
                config = await learner.put(
                    f"{AGENT}/skills/{skill}",
                    json={
                        "enabled": True,
                        "authorized_tools": focused["available_tools"],
                        "parameters": {
                            "tags": ["背包"],
                            "minimum_difficulty": "hard",
                            "maximum_difficulty": "hard",
                            "daily_minutes": 20,
                        },
                    },
                )
                assert config.status_code == 200, config.text
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                passed = await grade(owner, foundation, None)
                missing = await start(learner, skill)
                await finish(passed.evaluation_id, "accepted")
                assert await worker.run_once()
                absent = (await learner.get(missing)).json()
                assert absent["state"] == "succeeded" and absent["result"]["items"] == []
                assert any("动态规划" in item for item in absent["result"]["gaps"])

                resolved = await recommend(learner)
                (candidate,) = resolved["items"]
                assert candidate["problem_version_id"] == str(target)
                (proof,) = candidate["prerequisites"]
                assert proof["tag"] == "动态规划" and proof["metadata_revision"] == 1
                assert proof["evidence"]["submission_id"] == str(passed.submission_id)
                assert resolved["prerequisite_gap"] is None

                plan = await learner.post(
                    f"{ROOT}/trainings",
                    json={"tags": ["背包"], "count": 1, "idempotency_key": str(uuid4())},
                )
                assert plan.status_code == 201, plan.text
                assert plan.json()["items"][0]["prerequisites"] == [proof]
                assert (await other.get(f"{ROOT}/trainings/{plan.json()['id']}")).status_code == 404
                run = await start(learner, skill)
                assert await worker.run_once()
                ready = (await learner.get(run)).json()
                assert ready["state"] == "succeeded", ready
                assert ready["result"]["items"][0]["prerequisites"] == [proof]
                async with operator.transaction():
                    await operator.execute(
                        "UPDATE problems SET enabled=FALSE WHERE active_version_id=$1",
                        foundation,
                    )
                assert (await learner.post(f"{run}/training")).status_code == 409
                assert (await recommend(learner))["items"] == []
                saved = await learner.get(f"{ROOT}/trainings/{plan.json()['id']}")
                assert saved.json()["items"][0]["prerequisites"] == [proof]
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
