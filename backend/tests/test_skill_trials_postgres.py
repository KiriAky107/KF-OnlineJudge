"""Exercise private authoring trials with the real platform and Worker roles."""

import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.modules.learning.agent_queue import AgentQueue
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_private_trial_pins_author_revision_and_reuses_authorized_tools(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        worker_engine = create_async_engine(postgres_url)
        worker_sessions = async_sessionmaker(worker_engine, expire_on_commit=False)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def http():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        async def login(client, username):
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account(username, password)
            logged = await client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert logged.status_code == 200, logged.text
            client.headers["x-csrf-token"] = client.cookies["kf_csrf"]
            return account.account_id

        async def start(client, version):
            session = await client.post(f"{ROOT}/sessions", json={"purpose": "focused_training"})
            assert session.status_code == 201
            return await client.post(
                f"{ROOT}/sessions/{session.json()['session_id']}/runs",
                json={"request_id": str(uuid4()), "skill_version_id": version},
            )

        try:
            async with app.router.lifespan_context(app), http() as author, http() as reader:
                owner = await login(author, "trial_author")
                other = await login(reader, "trial_reader")
                problem = await app.state.problems.import_directory(tmp_path / "import")
                async with app.state.sessions.begin() as db:
                    db.add(
                        ProblemLearningMetadata(
                            problem_version_id=problem,
                            tags=["动态规划"],
                            difficulty="easy",
                            revision=1,
                        )
                    )
                    assert not await db.scalar(
                        text("SELECT has_table_privilege(current_user,'skills_trials','UPDATE')")
                    )
                tools = [{"name": "training_candidates", "version": 1}]
                document = {
                    "purpose": "focused_training",
                    "title": "私有动态规划训练",
                    "description": "仅在作者账号试用",
                    "method": "仅在编辑器中显示的教学方法。",
                    "parameters": {
                        "tags": ["动态规划"],
                        "goal": "修订零目标",
                        "daily_minutes": 20,
                        "maximum_difficulty": "easy",
                    },
                    "requested_tools": tools,
                }
                made = await author.post(
                    f"{ROOT}/skill-drafts",
                    json={"request_id": str(uuid4()), "document": document},
                )
                assert made.status_code == 201, made.text
                draft = f"{ROOT}/skill-drafts/{made.json()['draft_id']}"
                trials = f"{draft}/trials"
                assert (await reader.post(trials, json={"revision": 0})).status_code == 404
                assert (
                    await author.post(trials, json={"revision": 0}, headers={"x-csrf-token": ""})
                ).status_code == 403
                concurrent = await asyncio.gather(
                    *(author.post(trials, json={"revision": 0}) for _ in range(3))
                )
                assert all(receipt.status_code == 201 for receipt in concurrent)
                initial = concurrent[0].json()
                assert all(receipt.json() == initial for receipt in concurrent)
                assert initial["content_digest"] == made.json()["content_digest"]

                version = initial["version_id"]
                skill = f"{ROOT}/skills/{version}"
                preferences = {"enabled": True, "authorized_tools": tools, "parameters": {}}
                assert (
                    await reader.get(f"{ROOT}/skills", params={"version_id": version})
                ).status_code == 404
                assert (await reader.put(skill, json=preferences)).status_code == 404
                assert (await start(reader, version)).status_code == 404
                library = await author.get(f"{ROOT}/skill-library")
                assert library.status_code == 200 and version not in library.text
                unbound = await start(author, version)
                assert unbound.status_code == 404
                assert (
                    await author.put(
                        skill,
                        json=preferences
                        | {"authorized_tools": [{"name": "submission_history", "version": 1}]},
                    )
                ).status_code == 422
                bound = await author.put(skill, json=preferences)
                assert (
                    bound.status_code == 200 and bound.json()["parameters"]["daily_minutes"] == 20
                )
                queued = await start(author, version)
                assert queued.status_code == 202, queued.text
                run_id = UUID(queued.json()["run_id"])
                run = f"{ROOT}/runs/{run_id}"

                for previous in range(2):
                    changed = await author.put(
                        draft,
                        json={
                            "revision": previous,
                            "document": document
                            | {
                                "title": f"后续修订 {previous + 1}",
                                "parameters": document["parameters"] | {"daily_minutes": 60},
                            },
                        },
                    )
                    assert changed.status_code == 200, changed.text
                assert (await author.post(trials, json={"revision": 1})).status_code == 409
                assert (await author.post(trials, json={"revision": 0})).json() == initial
                newer = await author.post(trials, json={"revision": 2})
                assert newer.status_code == 201, newer.text
                replacement = newer.json()["version_id"]
                assert replacement != version
                assert newer.json()["content_digest"] != initial["content_digest"]
                changed_binding = await author.put(f"{ROOT}/skills/{replacement}", json=preferences)
                assert changed_binding.json()["parameters"]["daily_minutes"] == 60

                projection = text(
                    "SELECT declaration FROM public.kf_read_claimed_skill_trial("
                    ":run, :revision, :version, :owner)"
                )
                args = {"run": run_id, "revision": 1, "version": UUID(version), "owner": owner}
                async with worker_engine.connect() as db:
                    assert not await db.scalar(
                        text("SELECT has_table_privilege(current_user,'skills_trials','SELECT')")
                    )
                    assert await db.scalar(projection, args) is None

                claim = AgentQueue.claim

                async def check_projection(queue):
                    lease = await claim(queue)
                    assert lease is not None and lease.run.run_id == run_id
                    valid = args | {"revision": lease.run.revision}
                    async with worker_engine.connect() as db:
                        data = await db.scalar(projection, valid)
                        assert set(data) == {"purpose", "parameters", "requested_tools"}
                        assert data["parameters"]["daily_minutes"] == 20
                        for wrong in (
                            {"owner": other},
                            {"version": UUID(replacement)},
                            {"revision": lease.run.revision - 1},
                        ):
                            assert await db.scalar(projection, valid | wrong) is None
                    return lease

                monkeypatch.setattr(AgentQueue, "claim", check_projection)
                assert await LearningWorker(worker_sessions).run_once()
                completed = (await author.get(run)).json()
                assert completed["state"] == "succeeded", completed
                assert completed["skill_version_id"] == version
                result = completed["result"]
                assert result["daily_minutes"] == 20 and result["goal"] == "修订零目标"
                assert [p["problem_version_id"] for p in result["items"]] == [str(problem)]
                assert (await reader.get(run)).status_code == 404
                assert (await author.get(f"{run}/trace")).json()["finished_tool_calls"] == 1
                async with worker_engine.connect() as db:
                    assert await db.scalar(projection, args) is None
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
