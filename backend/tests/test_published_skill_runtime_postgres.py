"""Run a reviewed learning skill with the actual restricted PostgreSQL worker role."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from conftest import connect
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_published_skill_configuration_keeps_runs_on_explicitly_selected_versions(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = await connect(postgres_url)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def http():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        async def login(client, username, *, reviewer=False):
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account(username, password)
            if reviewer:
                await set_administrator(operator, account.account_id, True)
            result = await client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert result.status_code == 200
            client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

        async def review(reviewer, release):
            result = await reviewer.post(
                f"/api/v1/admin/skill-reviews/{release}", json={"action": "publish"}
            )
            assert result.status_code == 200, result.text

        async def submit(author, draft, revision):
            result = await author.post(
                f"{draft}/releases", json={"revision": revision, "confirmed_public": True}
            )
            assert result.status_code == 201, result.text
            return result.json()["version_id"]

        async def start(client, release):
            session = await client.post(f"{ROOT}/sessions", json={"purpose": "periodic_review"})
            assert session.status_code == 201
            return await client.post(
                f"{ROOT}/sessions/{session.json()['session_id']}/runs",
                json={"request_id": str(uuid4()), "skill_version_id": release},
            )

        try:
            async with (
                app.router.lifespan_context(app),
                http() as author,
                http() as learner,
                http() as reviewer,
            ):
                await login(author, "runtime_author")
                await login(learner, "runtime_reader")
                await login(reviewer, "runtime_reviewer", reviewer=True)
                problem = await app.state.problems.import_directory(tmp_path / "import")
                tools = [
                    {"name": "submission_history", "version": 1},
                    {"name": "training_candidates", "version": 1},
                ]
                document = {
                    "purpose": "periodic_review",
                    "title": "按三周回顾",
                    "description": "用真实历史与候选题安排复习",
                    "method": "检查证据并安排复习。",
                    "license": "CC BY 4.0",
                    "parameters": {"cycle_days": 21, "sessions_per_week": 2},
                    "requested_tools": tools,
                }
                made = await author.post(
                    f"{ROOT}/skill-drafts",
                    json={"request_id": str(uuid4()), "document": document},
                )
                assert made.status_code == 201, made.text
                draft = f"{ROOT}/skill-drafts/{made.json()['draft_id']}"
                original = await submit(author, draft, revision=0)
                skills = f"{ROOT}/skills"
                own = f"{skills}/{original}"
                preferences = {"enabled": True, "authorized_tools": tools, "parameters": {}}
                assert (
                    await learner.get(skills, params={"version_id": original})
                ).status_code == 404
                assert (await learner.put(own, json=preferences)).status_code == 404
                runtime_projection = text("SELECT * FROM public.kf_read_published_skill(:id)")
                async with worker_engine.connect() as db:
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "current_user,'skills_publications','SELECT')"
                        )
                    )
                    assert not (await db.execute(runtime_projection, {"id": original})).first()
                await review(reviewer, original)

                directory = await learner.get(skills, params={"version_id": original})
                assert directory.status_code == 200, directory.text
                entry = next(v for v in directory.json()["versions"] if v["version_id"] == original)
                assert entry["origin"] == "published" and entry["binding"] is None
                assert entry["default_parameters"]["cycle_days"] == 21
                assert entry["available_tools"] == tools
                assert (
                    await learner.put(
                        own,
                        json=preferences
                        | {"authorized_tools": [{"name": "source_evidence", "version": 1}]},
                    )
                ).status_code == 422
                bound = await learner.put(own, json=preferences)
                assert bound.status_code == 200, bound.text
                assert bound.json()["parameters"]["cycle_days"] == 21
                assert (await start(author, original)).status_code == 404
                pending = await start(learner, original)
                assert pending.status_code == 202, pending.text
                run_url = f"{ROOT}/runs/{pending.json()['run_id']}"

                change = await author.put(
                    draft,
                    json={
                        "revision": 0,
                        "document": document
                        | {
                            "title": "按六周回顾",
                            "parameters": {"cycle_days": 42, "sessions_per_week": 3},
                        },
                    },
                )
                assert change.status_code == 200
                newer = await submit(author, draft, revision=change.json()["revision"])
                await review(reviewer, newer)
                current = (await learner.get(skills)).json()["versions"]
                assert [v["version_id"] for v in current if v["origin"] == "published"] == [
                    original
                ]
                newer_url = f"{skills}/{newer}"
                swapped = await learner.put(newer_url, json=preferences)
                assert swapped.status_code == 200, swapped.text
                assert swapped.json()["parameters"]["cycle_days"] == 42

                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                assert await worker.run_once()
                done = (await learner.get(run_url)).json()
                assert done["skill_version_id"] == original and done["state"] == "succeeded", done
                assert (
                    done["result"]["cycle_days"] == 21 and done["result"]["sessions_per_week"] == 2
                )
                assert done["result"]["items"][0]["problem_version_id"] == str(problem)
                assert (await learner.get(f"{run_url}/trace")).json()["finished_tool_calls"] == 2
                assert (await start(learner, original)).status_code == 404
                queued = await start(learner, newer)
                assert queued.status_code == 202
                assert (
                    await learner.put(newer_url, json=preferences | {"enabled": False})
                ).status_code == 200
                assert await worker.run_once()
                stopped = (await learner.get(f"{ROOT}/runs/{queued.json()['run_id']}")).json()
                assert (
                    stopped["state"] == "failed"
                    and stopped["failure_code"] == "authorization_unavailable"
                )
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
