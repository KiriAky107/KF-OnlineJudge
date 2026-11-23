"""Save the exact training set reviewed by its owner."""

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

AGENT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_training_set_revision_and_confirmation_use_its_reviewed_order(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
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
                await app.state.identity.create_account("training_set_learner", password)
                login = await browser.post(
                    "/api/v1/auth/login",
                    json={"username": "training_set_learner", "password": password},
                )
                assert login.status_code == 200
                browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]

                versions = set()
                for slug in ("search-first", "search-second", "search-third"):
                    directory = tmp_path / slug
                    import_fixture(directory, slug=slug)
                    version = await app.state.problems.import_directory(directory)
                    versions.add(str(version))
                    async with app.state.sessions.begin() as db:
                        db.add(
                            ProblemLearningMetadata(
                                problem_version_id=version,
                                tags=["二分查找"],
                                difficulty="easy",
                                revision=1,
                            )
                        )

                catalog = (await browser.get(f"{AGENT}/skills")).json()["versions"]
                skill = next(item for item in catalog if item["purpose"] == "focused_training")
                skill_id = skill["version_id"]
                binding_path = f"{AGENT}/skills/{skill_id}"
                binding = await browser.put(
                    binding_path,
                    json={
                        "enabled": True,
                        "authorized_tools": skill["available_tools"],
                        "parameters": {
                            "tags": ["图论"],
                            "minimum_difficulty": "hard",
                            "maximum_difficulty": "hard",
                            "daily_minutes": 20,
                        },
                    },
                )
                assert binding.status_code == 200, binding.text

                async def session_path():
                    response = await browser.post(
                        f"{AGENT}/sessions", json={"purpose": "focused_training"}
                    )
                    assert response.status_code == 201, response.text
                    return f"{AGENT}/sessions/{response.json()['session_id']}/runs"

                path = await session_path()
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                draft = {
                    "title": "二分入门",
                    "tags": ["二分查找"],
                    "difficulty": "easy",
                    "count": 2,
                }

                async def send_draft(target, value):
                    return await browser.post(
                        target,
                        json={
                            "request_id": str(uuid4()),
                            "skill_version_id": skill_id,
                            "training_set": value,
                        },
                    )

                async def complete(value):
                    accepted = await send_draft(path, value)
                    assert accepted.status_code == 202, accepted.text
                    run = f"{AGENT}/runs/{accepted.json()['run_id']}"
                    assert await worker.run_once()
                    result = (await browser.get(run)).json()
                    assert result["state"] == "succeeded", result
                    return run, result

                original_path, original = await complete(draft)
                selected = [item["problem_version_id"] for item in original["result"]["items"]]
                assert len(selected) == 2 and set(selected) < versions
                revision = draft | {
                    "title": "练习二分",
                    "revision": {"run_id": original["run_id"], "replace_position": 2},
                }
                other_session = await send_draft(await session_path(), revision)
                assert other_session.status_code == 404
                changed_path, changed = await complete(revision)
                reviewed = [item["problem_version_id"] for item in changed["result"]["items"]]
                (replacement,) = versions.difference(selected)
                assert reviewed == [selected[0], replacement]
                assert (await browser.get(original_path)).json() == original

                first, second = await asyncio.gather(
                    browser.post(f"{changed_path}/training"),
                    browser.post(f"{changed_path}/training"),
                )
                assert first.status_code == second.status_code == 200, (first.text, second.text)
                assert first.json() == second.json()
                plan = first.json()
                assert plan["title"] == "练习二分"
                assert [item["problem_version_id"] for item in plan["items"]] == reviewed
                assert (await browser.get(changed_path)).json()["training_id"] == plan["id"]
                assert (await browser.get(f"/api/v1/trainings/{plan['id']}")).json() == plan
                saved_skill = next(
                    item
                    for item in (await browser.get(f"{AGENT}/skills")).json()["versions"]
                    if item["version_id"] == skill_id
                )
                assert saved_skill["binding"]["parameters"] == binding.json()["parameters"]
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
