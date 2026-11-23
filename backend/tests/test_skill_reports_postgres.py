"""Verify that live reviewers and the reporter are the only recipients of a report."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from conftest import connect
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

ROOT = "/api/v1/learning/agent"
REPORTS = "/api/v1/admin/skill-reports"
ORIGIN = "http://localhost:5173"


def test_skill_reports_keep_details_private_and_resolve_one_pending_item(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = await connect(postgres_url)

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

        try:
            async with (
                app.router.lifespan_context(app),
                client() as author,
                client() as reporter,
                client() as reviewer,
            ):
                await login(author, "report_author")
                await login(reporter, "report_reader")
                await login(reviewer, "report_administrator", admin=True)
                draft = await author.post(
                    f"{ROOT}/skill-drafts",
                    json={
                        "request_id": str(uuid4()),
                        "document": {
                            "purpose": "guided_help",
                            "title": "示例答疑方法",
                            "description": "从题目约束入手。",
                            "method": "先检查边界。",
                            "license": "CC BY 4.0",
                        },
                    },
                )
                assert draft.status_code == 201
                sent = await author.post(
                    f"{ROOT}/skill-drafts/{draft.json()['draft_id']}/releases",
                    json={"revision": 0, "confirmed_public": True},
                )
                assert sent.status_code == 201
                version = sent.json()["version_id"]
                detail = f"{ROOT}/skill-library/{version}"
                path = detail + "/reports"
                body = {
                    "request_id": str(uuid4()),
                    "reason": "incorrect_content",
                    "details": "这里仅是举报人可见的示例。",
                }
                assert (await reporter.post(path, json=body)).status_code == 404
                response = await reviewer.post(
                    f"/api/v1/admin/skill-reviews/{version}", json={"action": "publish"}
                )
                assert response.status_code == 200
                assert (
                    await reporter.post(path, json=body, headers={"x-csrf-token": ""})
                ).status_code == 403
                replies = await asyncio.gather(*(reporter.post(path, json=body) for _ in range(2)))
                assert all(item.status_code == 201 for item in replies)
                state = replies[0].json()
                assert replies[1].json() == state and state["state"] == "pending"
                assert (await reporter.get(path)).json()["items"] == [state]
                assert (await author.get(path)).json()["items"] == []
                assert body["details"] not in (await author.get(detail)).text
                assert (await reporter.get(REPORTS)).status_code == 403
                assert (await reviewer.get(REPORTS)).json()["items"] == [
                    state | {"release_title": "示例答疑方法", "version": 1}
                ]
                assert (
                    await reporter.post(path, json=body | {"details": "由不同正文重用旧凭据"})
                ).status_code == 409
                assert (
                    await reporter.post(path, json=body | {"request_id": str(uuid4())})
                ).status_code == 409

                action = f"{REPORTS}/{state['report_id']}"
                decision = {"action": "confirm", "note": "确认存在不准确的说明。"}
                assert (await author.post(action, json=decision)).status_code == 403
                assert (
                    await reviewer.post(action, json=decision | {"note": "   "})
                ).status_code == 422
                confirmed = await reviewer.post(action, json=decision)
                assert confirmed.status_code == 200, confirmed.text
                assert (await reviewer.post(action, json=decision)).json() == confirmed.json()
                assert (
                    await reviewer.post(action, json=decision | {"action": "dismiss"})
                ).status_code == 409
                assert (await reviewer.get(REPORTS)).json()["items"] == []
                history = (await reporter.get(path)).json()["items"]
                assert len(history) == 1 and history[0]["state"] == "confirmed"
                assert history[0]["decision_note"] == decision["note"]
                assert (await reporter.post(path, json=body)).json() == history[0]
                assert (await author.get(path)).json()["items"] == []
                assert decision["note"] not in (await author.get(detail)).text

                next_report = await reporter.post(
                    path, json=body | {"request_id": str(uuid4()), "reason": "licensing"}
                )
                assert next_report.status_code == 201
                assert next_report.json()["report_id"] != state["report_id"]
                assert not await operator.fetchval(
                    "SELECT has_column_privilege('kf_oj_platform', 'skills_reports', "
                    "'details', 'UPDATE')"
                )
                assert not await operator.fetchval(
                    "SELECT has_table_privilege("
                    "'kf_oj_learning_worker', 'skills_reports', 'SELECT')"
                )
        finally:
            await operator.close()

    asyncio.run(exercise())
