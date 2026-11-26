import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from conftest import connect
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

from kf_oj.infra.clangd_completion import SemanticCandidate

ORIGIN = "http://localhost:5173"
POLICY = "/api/v1/admin/contest-schedules/editor-cup/editor-policy"
PROBLEMS = "/api/v1/contests/editor-cup/editor-problems"


def test_editor_policy_controls_registered_accounts_and_assigned_versions(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        calls = []

        class Analyzer:
            async def complete(self, *request):
                calls.append(request)
                return (SemanticCandidate("push_back", "method"),)

        operator = await connect(postgres_url)
        try:
            async with app.router.lifespan_context(app):
                app.state.editor_completion = Analyzer()
                app.state.editor_completion_slots = asyncio.Semaphore(2)
                app.state.editor_completion_busy = set()
                transport = httpx.ASGITransport(app)

                def client():
                    return httpx.AsyncClient(
                        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
                    )

                async def login(http, name, *, admin=False):
                    password = secrets.token_urlsafe(24)
                    identity = await app.state.identity.create_account(name, password)
                    if admin:
                        await set_administrator(operator, identity.account_id, True)
                    response = await http.post(
                        "/api/v1/auth/login", json={"username": name, "password": password}
                    )
                    assert response.status_code == 200
                    http.headers["x-csrf-token"] = response.json()["csrf_token"]

                async with client() as admin, client() as entrant, client() as practice:
                    await login(admin, "editor_admin", admin=True)
                    await login(entrant, "editor_entrant")
                    await login(practice, "editor_practice")
                    version = await app.state.problems.import_directory(tmp_path / "import")
                    editor = f"/api/v1/problems/versions/{version}/editor-completion"
                    payload = {
                        "language": "cpp20",
                        "source": "void f() { arr. }",
                        "line": 0,
                        "character": 15,
                    }
                    now = datetime.now(UTC)
                    scheduled = await admin.post(
                        "/api/v1/admin/contest-schedules",
                        json={
                            "key": "editor-cup",
                            "title": "Editor Cup",
                            "starts_at": (now + timedelta(minutes=3)).isoformat(),
                            "ends_at": (now + timedelta(hours=1)).isoformat(),
                        },
                    )
                    assert scheduled.status_code == 201
                    policy = {"semantic_completion_enabled": False, "version_ids": [str(version)]}
                    assert (await entrant.put(POLICY, json=policy)).status_code == 403
                    unknown = policy | {"version_ids": [str(uuid4())]}
                    assert (await admin.put(POLICY, json=unknown)).status_code == 409
                    saved = await admin.put(POLICY, json=policy)
                    assert saved.status_code == 200, saved.text
                    assert (await admin.get(POLICY)).json() == policy
                    response = await entrant.put("/api/v1/contests/editor-cup/registration")
                    assert response.json() == {"confirmed": True}
                    assert (await entrant.get(PROBLEMS)).json() == []
                    assert (await entrant.get(editor)).json() == {"enabled": True}

                    await operator.execute(
                        "UPDATE scheduled_contests SET starts_at=$1 WHERE key='editor-cup'",
                        now - timedelta(minutes=1),
                    )
                    assert (await entrant.get(PROBLEMS)).json()[0]["version_id"] == str(version)
                    assert (await practice.get(PROBLEMS)).json() == []
                    assert (await entrant.get(editor)).json() == {"enabled": False}
                    assert (await entrant.post(editor, json=payload)).json() == []
                    assert not calls
                    assert (await practice.post(editor, json=payload)).json() == [
                        {"name": "push_back", "kind": "method"}
                    ]
                    assert len(calls) == 1

                    change = policy | {"semantic_completion_enabled": True}
                    assert (await admin.put(POLICY, json=change)).json() == change
                    assert (await entrant.post(editor, json=payload)).json()[0][
                        "name"
                    ] == "push_back"
                    assert len(calls) == 2
        finally:
            await operator.close()

    asyncio.run(exercise())
