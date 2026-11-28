"""Team teaching exposes topic-level aggregates with a participation floor."""

import asyncio
import secrets
from contextlib import AsyncExitStack
from uuid import UUID, uuid4

import httpx
from conftest import connect
from test_submission_create import setup_app

_ORIGIN = "http://localhost:5173"


def test_team_teaching_ignores_private_history_and_small_groups(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)

        async with AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            version = await app.state.problems.import_directory(tmp_path / "import")
            db = await connect(postgres_url)
            stack.push_async_callback(db.close)

            async def member(username):
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url=_ORIGIN,
                        headers={"origin": _ORIGIN},
                    )
                )
                password = secrets.token_urlsafe(24)
                account = await app.state.identity.create_account(username, password)
                login = await client.post(
                    "/api/v1/auth/login", json={"username": username, "password": password}
                )
                assert login.status_code == 200, login.status_code
                client.headers["x-csrf-token"] = client.cookies["kf_csrf"]
                return client, account.account_id

            async def finished(client, verdict):
                result = await client.post(
                    "/api/v1/submissions",
                    json={
                        "problem_version_id": str(version),
                        "language": "c17",
                        "source": "int main(void) { return 0; }",
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert result.status_code == 201, result.text
                await db.execute(
                    "UPDATE evaluations SET state='finished',verdict=$1,"
                    "updated_at=clock_timestamp() WHERE id=$2",
                    verdict,
                    UUID(result.json()["evaluation_id"]),
                )

            captain, _ = await member("review_captain")
            outsiders, _ = await member("review_outside")
            learners = [await member(f"review_member_{i}") for i in range(5)]
            team = await captain.post(
                "/api/v1/teams",
                json={
                    "name": "图论小班",
                    "description": "同组练习",
                    "idempotency_key": str(uuid4()),
                },
            )
            assert team.status_code == 201, team.text
            path = f"/api/v1/teams/{team.json()['id']}"
            private = await captain.post(
                "/api/v1/trainings",
                json={"title": "团队图论", "count": 1, "idempotency_key": str(uuid4())},
            )
            assert private.status_code == 201, private.text
            shared = await captain.post(
                f"/api/v1/trainings/{private.json()['id']}/publish-team/{team.json()['id']}",
                json={"confirmed": True},
            )
            assert shared.status_code == 201, shared.text
            publication = f"{path}/trainings/{shared.json()['id']}"
            review_url = publication + "/review"

            for client, account in learners:
                assert (await client.put(path + "/membership")).status_code == 200
                assert (
                    await captain.put(f"{path}/members/{account}", json={"decision": "approve"})
                ).status_code == 200
            fifth, fifth_id = learners[-1]
            await finished(fifth, "wrong_answer")
            for client, _ in learners:
                assert (await client.post(publication + "/start")).status_code == 201
            assert (await captain.post(publication + "/start")).status_code == 201
            assert (await fifth.get(review_url)).status_code == 403
            assert (await outsiders.get(review_url)).status_code == 403

            for client, _ in learners[:4]:
                await finished(client, "wrong_answer")
            await finished(learners[0][0], "wrong_answer")
            await finished(outsiders, "wrong_answer")
            await finished(captain, "wrong_answer")
            assert (await captain.get(review_url)).json() == {
                "state": "insufficient_data",
                "items": [],
            }

            await finished(fifth, "wrong_answer")
            result = await captain.get(review_url)
            assert result.status_code == 200, result.text
            expected = {
                "state": "ready",
                "items": [
                    {
                        "position": 0,
                        "title": shared.json()["items"][0]["title"],
                        "topic": "correctness",
                    }
                ],
            }
            assert result.json() == expected

            await finished(fifth, "accepted")
            await finished(fifth, "wrong_answer")
            assert (await captain.get(review_url)).json() == {"state": "ready", "items": []}
            assert (
                await captain.put(f"{path}/members/{fifth_id}", json={"decision": "remove"})
            ).status_code == 200
            assert (await captain.get(review_url)).json() == {
                "state": "insufficient_data",
                "items": [],
            }

    asyncio.run(exercise())
