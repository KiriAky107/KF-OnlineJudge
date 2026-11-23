"""Verify author archives and administrator holds using real bindings and the learning Worker."""

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
REVIEWS = "/api/v1/admin/skill-reviews"
ORIGIN = "http://localhost:5173"


def test_admin_hold_disables_discovery_new_and_queued_runs_for_one_version(
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

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            )

        async def login(http, name, *, admin=False):
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account(name, password)
            if admin:
                await set_administrator(operator, account.account_id, True)
            signed_in = await http.post(
                "/api/v1/auth/login", json={"username": name, "password": password}
            )
            assert signed_in.status_code == 200, signed_in.text
            http.headers["x-csrf-token"] = http.cookies["kf_csrf"]

        async def submit(author, draft_path, revision):
            response = await author.post(
                draft_path + "/releases", json={"revision": revision, "confirmed_public": True}
            )
            assert response.status_code == 201
            return response.json()

        async def new_run(learner, version_id):
            session = await learner.post(f"{ROOT}/sessions", json={"purpose": "guided_help"})
            assert session.status_code == 201
            return await learner.post(
                f"{ROOT}/sessions/{session.json()['session_id']}/runs",
                json={"request_id": str(uuid4()), "skill_version_id": version_id},
            )

        try:
            async with (
                app.router.lifespan_context(app),
                client() as author,
                client() as learner,
                client() as admin,
            ):
                await login(author, "hold_author")
                await login(learner, "hold_reader")
                await login(admin, "hold_administrator", admin=True)
                tools = [{"name": "submission_history", "version": 1}]
                doc = {
                    "purpose": "guided_help",
                    "title": "待改正的示例提示",
                    "description": "根据历史证据提供提示。",
                    "method": "先核对边界。",
                    "license": "CC BY 4.0",
                    "requested_tools": tools,
                }
                made = await author.post(
                    f"{ROOT}/skill-drafts", json={"request_id": str(uuid4()), "document": doc}
                )
                assert made.status_code == 201
                draft_path = f"{ROOT}/skill-drafts/{made.json()['draft_id']}"
                original = await submit(author, draft_path, 0)
                version = original["version_id"]
                hold_url = f"{REVIEWS}/{version}/disable"
                archive_url = f"{ROOT}/skill-versions/{version}/archive"
                action = {"reason": "公开说明包含错误，请作者发布修订。"}
                assert (await admin.post(hold_url, json=action)).status_code == 404
                assert (await author.post(archive_url)).status_code == 409
                approved = await admin.post(f"{REVIEWS}/{version}", json={"action": "publish"})
                assert approved.status_code == 200
                assert (await learner.post(hold_url, json=action)).status_code == 403
                assert (
                    await admin.post(hold_url, json=action, headers={"x-csrf-token": ""})
                ).status_code == 403

                binding = {"enabled": True, "parameters": {}, "authorized_tools": tools}
                bound = await learner.put(f"{ROOT}/skills/{version}", json=binding)
                assert bound.status_code == 200, bound.text

                assert (await learner.post(archive_url)).status_code == 404
                assert (
                    await author.post(archive_url, headers={"x-csrf-token": ""})
                ).status_code == 403
                first, repeat = await asyncio.gather(*(author.post(archive_url) for _ in range(2)))
                assert first.status_code == repeat.status_code == 200
                archive = first.json()
                assert archive == repeat.json()
                assert archive["state"] == "archived" and archive["archived_at"]
                assert archive["document"] == original["document"]
                assert archive["content_digest"] == original["content_digest"]
                assert (await admin.get(REVIEWS, params={"state": "archived"})).json()["items"] == [
                    archive
                ]
                assert (await admin.get(REVIEWS, params={"state": "published"})).json()[
                    "items"
                ] == []
                assert (await learner.get(f"{ROOT}/skill-library/{version}")).status_code == 404
                assert (await learner.get(f"{ROOT}/skill-library")).json()["items"] == []
                assert (
                    await author.get(f"{ROOT}/skills", params={"version_id": version})
                ).status_code == 404
                assert (
                    await author.put(f"{ROOT}/skills/{version}", json=binding)
                ).status_code == 404
                assert (
                    await author.post(
                        f"{ROOT}/skill-drafts",
                        json={"source_version_id": version, "request_id": str(uuid4())},
                    )
                ).status_code == 404
                existing = next(
                    item
                    for item in (await learner.get(f"{ROOT}/skills")).json()["versions"]
                    if item["version_id"] == version
                )
                assert existing["archived_at"] == archive["archived_at"]
                assert existing["binding"]["enabled"]
                assert (
                    await learner.put(f"{ROOT}/skills/{version}", json=binding)
                ).status_code == 200
                running = await new_run(learner, version)
                assert running.status_code == 202
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                assert await worker.run_once()
                old_url = f"{ROOT}/runs/{running.json()['run_id']}"
                baseline = await learner.get(old_url)
                assert baseline.json()["state"] == "succeeded", baseline.text
                queued = await new_run(learner, version)
                assert queued.status_code == 202
                queued_url = f"{ROOT}/runs/{queued.json()['run_id']}"

                stopped = await admin.post(hold_url, json=action)
                assert stopped.status_code == 200, stopped.text
                view = stopped.json()
                assert view["state"] == "disabled" and view["disable_reason"] == action["reason"]
                assert view["content_digest"] == original["content_digest"] and view["disabled_at"]
                assert view["archived_at"] == archive["archived_at"]
                assert (await admin.post(hold_url, json=action)).json() == view
                assert (
                    await admin.post(hold_url, json={"reason": "different reason"})
                ).status_code == 409
                assert (await admin.get(REVIEWS, params={"state": "disabled"})).json()["items"] == [
                    view
                ]
                assert (await author.get(draft_path + "/releases")).json()["items"] == [view]
                assert (await learner.get(f"{ROOT}/skill-library/{version}")).status_code == 404
                assert (await learner.get(f"{ROOT}/skill-library")).json()["items"] == []
                assert (
                    await learner.put(f"{ROOT}/skills/{version}", json=binding)
                ).status_code == 404
                assert (await new_run(learner, version)).status_code == 404
                assert (
                    await learner.post(
                        f"{ROOT}/skill-drafts",
                        json={"source_version_id": version, "request_id": str(uuid4())},
                    )
                ).status_code == 404
                assert (
                    await learner.put(f"{ROOT}/skill-library/{version}/rating", json={"score": 4})
                ).status_code == 404
                assert (
                    await learner.post(
                        f"{ROOT}/skill-library/{version}/reports",
                        json={
                            "request_id": str(uuid4()),
                            "reason": "incorrect_content",
                            "details": "test",
                        },
                    )
                ).status_code == 404

                assert await worker.run_once()
                refused = (await learner.get(queued_url)).json()
                assert (
                    refused["state"] == "failed"
                    and refused["failure_code"] == "authorization_unavailable"
                )
                assert (await learner.get(f"{queued_url}/trace")).json()["tool_calls"] == 0
                assert (await learner.get(old_url)).json() == baseline.json()
                async with worker_engine.connect() as db:
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "current_user, 'skills_version_holds', 'SELECT')"
                        )
                    )
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "current_user, 'skills_version_archives', 'SELECT')"
                        )
                    )

                corrected = await author.put(
                    draft_path,
                    json={"revision": 0, "document": doc | {"title": "修订后的示例提示"}},
                )
                newer = await submit(author, draft_path, corrected.json()["revision"])
                result = await admin.post(
                    f"{REVIEWS}/{newer['version_id']}", json={"action": "publish"}
                )
                assert result.status_code == 200
                active = (await learner.get(f"{ROOT}/skill-library")).json()["items"]
                assert [entry["version_id"] for entry in active] == [newer["version_id"]]
                assert (await new_run(learner, newer["version_id"])).status_code == 404
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
