"""Exercise real private ratings, recommendation changes, and immutable plan evidence."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata

ROOT = "/api/v1"
ORIGIN = "http://localhost:5173"


def test_training_feedback_changes_only_the_owners_future_candidates(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def http():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        try:
            async with (
                app.router.lifespan_context(app),
                http() as owner,
                http() as foreign,
            ):
                for client, name in ((owner, "rating_owner"), (foreign, "rating_other")):
                    password = secrets.token_urlsafe(24)
                    await app.state.identity.create_account(name, password)
                    login = await client.post(
                        f"{ROOT}/auth/login", json={"username": name, "password": password}
                    )
                    assert login.status_code == 200
                    client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

                easy = await app.state.problems.import_directory(tmp_path / "import")
                additional = tmp_path / "medium"
                import_fixture(additional, slug="medium-fixture")
                medium = await app.state.problems.import_directory(additional)
                async with app.state.sessions.begin() as session:
                    for version, level in ((easy, "easy"), (medium, "medium")):
                        session.add(
                            ProblemLearningMetadata(
                                problem_version_id=version,
                                tags=["排序"],
                                difficulty=level,
                                revision=1,
                            )
                        )

                base = {"tags": ["排序"], "count": 1}
                original = await owner.post(
                    f"{ROOT}/trainings",
                    json={
                        **base,
                        "difficulty": "easy",
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert original.status_code == 201
                plan = original.json()
                (item,) = plan["items"]
                assert item["problem_version_id"] == str(easy)
                assert item["progress"] == "not_started" and item["feedback"] is None
                feedback_path = f"{ROOT}/trainings/{plan['id']}/items/{item['id']}/feedback"
                assert (
                    await foreign.put(feedback_path, json={"rating": "too_hard"})
                ).status_code == 404
                rejected = await owner.put(
                    feedback_path,
                    json={"rating": "too_easy"},
                    headers={"x-csrf-token": ""},
                )
                assert rejected.status_code == 403
                rated = await owner.put(feedback_path, json={"rating": "too_easy"})
                assert rated.status_code == 200
                (rated_item,) = rated.json()["items"]
                assert rated_item["feedback"]["rating"] == "too_easy"
                assert (
                    rated_item["progress"] == "not_started" and not rated_item["progress_evidence"]
                )
                repeated = await owner.put(feedback_path, json={"rating": "too_easy"})
                assert repeated.json()["items"][0]["feedback"] == rated_item["feedback"]

                async def recommend(client, filters=base):
                    response = await client.post(f"{ROOT}/learning/recommendations", json=filters)
                    assert response.status_code == 200
                    return response.json()["items"][0]

                plain = await recommend(foreign)
                assert (
                    plain["problem_version_id"] == str(easy) and plain["feedback_evidence"] is None
                )
                adapted = await recommend(owner)
                assert adapted["problem_version_id"] == str(medium)
                source = adapted["feedback_evidence"]
                assert (
                    source["training_item_id"] == item["id"]
                    and source["preferred_difficulty"] == "medium"
                )
                forced = await recommend(owner, base | {"difficulty": "easy"})
                assert forced["problem_version_id"] == str(easy)

                agent = f"{ROOT}/learning/agent"
                versions = (await owner.get(f"{agent}/skills")).json()["versions"]
                focused = next(
                    entry for entry in versions if entry["purpose"] == "focused_training"
                )
                configured = await owner.put(
                    f"{agent}/skills/{focused['version_id']}",
                    json={
                        "enabled": True,
                        "authorized_tools": focused["available_tools"],
                        "parameters": {
                            "tags": ["排序"],
                            "daily_minutes": 20,
                            "maximum_difficulty": "medium",
                        },
                    },
                )
                assert configured.status_code == 200
                session_id = (
                    await owner.post(
                        f"{agent}/sessions",
                        json={
                            "purpose": "focused_training",
                        },
                    )
                ).json()["session_id"]
                started = await owner.post(
                    f"{agent}/sessions/{session_id}/runs",
                    json={
                        "skill_version_id": focused["version_id"],
                        "request_id": str(uuid4()),
                    },
                )
                assert started.status_code == 202
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                assert await worker.run_once()
                run = f"{agent}/runs/{started.json()['run_id']}"
                draft = (await owner.get(run)).json()
                assert draft["state"] == "succeeded"
                (training_item,) = draft["result"]["items"]
                assert training_item["problem_version_id"] == str(medium)
                assert training_item["feedback_evidence"] == source
                saved = await owner.post(run + "/training")
                assert saved.status_code == 200
                assert saved.json()["items"][0]["feedback_evidence"] == source

                cleared = await owner.put(feedback_path, json={"rating": None})
                assert cleared.status_code == 200 and cleared.json()["items"][0]["feedback"] is None
                after = await recommend(owner)
                assert (
                    after["problem_version_id"] == str(easy) and after["feedback_evidence"] is None
                )
                old = await owner.get(f"{ROOT}/trainings/{saved.json()['id']}")
                assert old.json()["items"][0]["feedback_evidence"] == source

                harder = tmp_path / "hard"
                import_fixture(harder, slug="hard-follow-up-fixture")
                hard_id = await app.state.problems.import_directory(harder)
                async with app.state.sessions.begin() as session:
                    session.add(
                        ProblemLearningMetadata(
                            problem_version_id=hard_id,
                            tags=["排序"],
                            difficulty="hard",
                            revision=1,
                        )
                    )
                saved_id = saved.json()["id"]
                (saved_item,) = saved.json()["items"]
                recent_feedback = await owner.put(
                    f"{ROOT}/trainings/{saved_id}/items/{saved_item['id']}/feedback",
                    json={"rating": "too_hard"},
                )
                assert recent_feedback.status_code == 200

                continuation = f"{ROOT}/trainings/{saved_id}/follow-ups"
                request = {"idempotency_key": str(uuid4())}
                assert (await foreign.post(continuation, json=request)).status_code == 404
                first, repeat = await asyncio.gather(
                    *(owner.post(continuation, json=request) for _ in range(2))
                )
                assert first.status_code == repeat.status_code == 201
                updated = first.json()
                assert updated == repeat.json()
                assert updated["id"] != saved_id and updated["previous_plan_id"] == saved_id
                assert updated["schedule"] == saved.json()["schedule"]
                (new_item,) = updated["items"]
                assert new_item["problem_version_id"] == str(easy)
                assert new_item["estimated_minutes"] == 20
                assert new_item["feedback_evidence"]["training_item_id"] == saved_item["id"]
                assert (await recommend(owner))["problem_version_id"] == str(hard_id)
                assert (
                    await owner.post(f"{ROOT}/trainings/{plan['id']}/follow-ups", json=request)
                ).status_code == 409
                reread = await owner.get(f"{ROOT}/trainings/{saved_id}")
                assert reread.json() == recent_feedback.json()
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
