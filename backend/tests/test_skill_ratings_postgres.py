"""Exercise version-specific ratings using PostgreSQL and real Cookie sessions."""

import asyncio
import secrets
from uuid import uuid4

import httpx
from conftest import connect
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_ratings_are_private_per_account_and_aggregated_by_published_version(
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

        async def log_in(http, name, *, admin=False):
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account(name, password)
            if admin:
                await set_administrator(operator, account.account_id, True)
            login = await http.post(
                "/api/v1/auth/login", json={"username": name, "password": password}
            )
            assert login.status_code == 200, login.text
            http.headers["x-csrf-token"] = http.cookies["kf_csrf"]

        async def submit(owner, path, revision):
            response = await owner.post(
                path + "/releases", json={"revision": revision, "confirmed_public": True}
            )
            assert response.status_code == 201
            return response.json()["version_id"]

        async def publish(admin, version):
            response = await admin.post(
                f"/api/v1/admin/skill-reviews/{version}", json={"action": "publish"}
            )
            assert response.status_code == 200, response.text

        def summary(version_id, count, average, own):
            return {"version_id": version_id, "count": count, "average": average, "own_score": own}

        try:
            async with (
                app.router.lifespan_context(app),
                client() as author,
                client() as learner,
                client() as reviewer,
                client() as anonymous,
            ):
                await log_in(author, "rating_author")
                await log_in(learner, "rating_learner")
                await log_in(reviewer, "rating_reviewer", admin=True)
                declaration = {
                    "purpose": "guided_help",
                    "title": "概念提示示例",
                    "description": "保留最小证据线索",
                    "method": "先复述约束，再给提示。",
                    "license": "CC BY 4.0",
                }
                made = await author.post(
                    f"{ROOT}/skill-drafts",
                    json={"request_id": str(uuid4()), "document": declaration},
                )
                assert made.status_code == 201
                draft = f"{ROOT}/skill-drafts/{made.json()['draft_id']}"
                old = await submit(author, draft, revision=0)
                rating = f"{ROOT}/skill-library/{old}/rating"
                assert (await anonymous.get(rating)).status_code == 401
                assert (await learner.get(rating)).status_code == 404
                assert (await learner.put(rating, json={"score": 4})).status_code == 404
                await publish(reviewer, old)
                assert (await learner.get(rating)).json() == summary(old, 0, None, None)
                assert (
                    await learner.put(rating, headers={"x-csrf-token": ""}, json={"score": 4})
                ).status_code == 403
                for invalid in (0, 6, True, "4"):
                    response = await learner.put(rating, json={"score": invalid})
                    assert response.status_code == 422

                repeated = await asyncio.gather(
                    *(learner.put(rating, json={"score": 4}) for _ in range(2))
                )
                assert all(response.json() == summary(old, 1, 4.0, 4) for response in repeated)
                assert (await author.get(rating)).json() == summary(old, 1, 4.0, None)
                assert (await reviewer.put(rating, json={"score": 5})).json() == summary(
                    old, 2, 4.5, 5
                )
                assert (await learner.put(rating, json={"score": 2})).json() == summary(
                    old, 2, 3.5, 2
                )
                assert (await learner.get(rating)).json() == summary(old, 2, 3.5, 2)
                assert (await reviewer.get(rating)).json() == summary(old, 2, 3.5, 5)
                assert (await author.delete(rating)).json() == summary(old, 2, 3.5, None)

                edited = await author.put(
                    draft, json={"revision": 0, "document": declaration | {"title": "新提示版本"}}
                )
                assert edited.status_code == 200
                new = await submit(author, draft, revision=edited.json()["revision"])
                await publish(reviewer, new)
                newer_rating = f"{ROOT}/skill-library/{new}/rating"
                assert (await learner.get(newer_rating)).json() == summary(new, 0, None, None)
                assert (await learner.put(newer_rating, json={"score": 3})).json() == summary(
                    new, 1, 3.0, 3
                )
                removed = await learner.delete(rating)
                assert removed.json() == summary(old, 1, 5.0, None)
                assert (await learner.delete(rating)).json() == removed.json()
                assert (
                    await operator.fetchval(
                        "SELECT count(*) FROM skills_ratings WHERE version_id=$1", old
                    )
                    == 1
                )
                assert not await operator.fetchval(
                    "SELECT has_table_privilege("
                    "'kf_oj_learning_worker', 'skills_ratings', 'SELECT')"
                )
        finally:
            await operator.close()

    asyncio.run(exercise())
