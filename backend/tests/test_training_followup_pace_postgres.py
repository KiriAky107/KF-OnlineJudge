"""Verify paced follow-up creation with real saved learning plans."""

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
AGENT = ROOT + "/learning/agent"
ORIGIN = "http://localhost:5173"


def test_follow_up_adjusts_pace_without_updating_the_source_plan(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as browser,
            ):
                password = secrets.token_urlsafe(24)
                await app.state.identity.create_account("pace_owner", password)
                login = await browser.post(
                    ROOT + "/auth/login", json={"username": "pace_owner", "password": password}
                )
                assert login.status_code == 200
                browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
                for slug in ("pace-exercise-one", "pace-exercise-two"):
                    directory = tmp_path / slug
                    import_fixture(directory, slug=slug)
                    version = await app.state.problems.import_directory(directory)
                    async with app.state.sessions.begin() as db:
                        db.add(
                            ProblemLearningMetadata(
                                problem_version_id=version,
                                tags=["数组"],
                                difficulty="easy",
                                revision=1,
                            )
                        )

                versions = (await browser.get(AGENT + "/skills")).json()["versions"]
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))

                async def initial(purpose, params):
                    skill = next(s for s in versions if s["purpose"] == purpose)
                    skill_id = skill["version_id"]
                    binding = await browser.put(
                        f"{AGENT}/skills/{skill_id}",
                        json={
                            "enabled": True,
                            "parameters": params,
                            "authorized_tools": skill["available_tools"],
                        },
                    )
                    assert binding.status_code == 200
                    session = await browser.post(AGENT + "/sessions", json={"purpose": purpose})
                    assert session.status_code == 201
                    queued = await browser.post(
                        f"{AGENT}/sessions/{session.json()['session_id']}/runs",
                        json={"skill_version_id": skill_id, "request_id": str(uuid4())},
                    )
                    assert queued.status_code == 202
                    assert await worker.run_once()
                    created = await browser.post(f"{AGENT}/runs/{queued.json()['run_id']}/training")
                    assert created.status_code == 200, created.text
                    return created.json()

                focused = await initial(
                    "focused_training",
                    {"goal": "复习数组边界", "tags": ["数组"], "daily_minutes": 20},
                )
                periodic = await initial(
                    "periodic_review",
                    {"stage_goal": "复习本周", "cycle_days": 21, "sessions_per_week": 1},
                )
                for source, pace, wrong_pace in (
                    (focused, {"daily_minutes": 40}, {"sessions_per_week": 2}),
                    (periodic, {"cycle_days": 14, "sessions_per_week": 2}, {"daily_minutes": 40}),
                ):
                    assert source["item_count"] == 1
                    path = f"{ROOT}/trainings/{source['id']}"
                    default = {"idempotency_key": str(uuid4())}
                    unchanged = await browser.post(path + "/follow-ups", json=default)
                    assert unchanged.status_code == 201, unchanged.text
                    assert unchanged.json()["schedule"] == source["schedule"]
                    assert unchanged.json()["item_count"] == 1
                    assert (
                        await browser.post(
                            path + "/follow-ups", json=default | {"daily_minutes": None}
                        )
                    ).json() == unchanged.json()
                    assert (
                        await browser.post(path + "/follow-ups", json=default | wrong_pace)
                    ).status_code == 409
                    assert (
                        await browser.post(
                            path + "/follow-ups",
                            json={"idempotency_key": str(uuid4()), **wrong_pace},
                        )
                    ).status_code == 409

                    body = {"idempotency_key": str(uuid4()), **pace}
                    first, replay = await asyncio.gather(
                        browser.post(path + "/follow-ups", json=body),
                        browser.post(path + "/follow-ups", json=body),
                    )
                    assert first.status_code == replay.status_code == 201, first.text
                    next_plan = first.json()
                    assert next_plan == replay.json()
                    assert next_plan["previous_plan_id"] == source["id"]
                    assert next_plan["schedule"] == source["schedule"] | pace
                    assert next_plan["parameters"]["tags"] == source["parameters"]["tags"]
                    assert next_plan["item_count"] == 2
                    assert (await browser.get(path)).json() == source
                    if "daily_minutes" in pace:
                        assert all(item["estimated_minutes"] == 20 for item in next_plan["items"])
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
