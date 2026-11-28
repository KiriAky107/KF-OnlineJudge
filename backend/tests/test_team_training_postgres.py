"""Team publication uses live captain and member roles through the public API."""

import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
from conftest import connect
from test_submission_create import setup_app

_ORIGIN = "http://localhost:5173"
_TEAMS = "/api/v1/teams"
_PRIVATE = "/api/v1/trainings"
_OFFICIAL = "/api/v1/official-trainings"


def test_team_training_has_one_scope_and_private_practice(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=_ORIGIN,
                headers={"origin": _ORIGIN},
            )

        async def authenticate(browser, username):
            password = secrets.token_urlsafe(24)
            user = await app.state.identity.create_account(username, password)
            response = await browser.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
            return str(user.account_id)

        async def create_team(browser, name):
            response = await browser.post(
                _TEAMS,
                json={"name": name, "description": "团队介绍", "idempotency_key": str(uuid4())},
            )
            assert response.status_code == 201, response.text
            return response.json()["id"]

        async def create_training(browser, title):
            response = await browser.post(
                _PRIVATE, json={"title": title, "count": 1, "idempotency_key": str(uuid4())}
            )
            assert response.status_code == 201, response.text
            return response.json()

        async with (
            app.router.lifespan_context(app),
            client() as captain,
            client() as learner,
            client() as outsider,
            client() as guest,
        ):
            version = await app.state.problems.import_directory(tmp_path / "import")
            await authenticate(captain, "training_captain")
            learner_id = await authenticate(learner, "training_learner")
            await authenticate(outsider, "training_outsider")
            team = await create_team(captain, "第一小组")
            second_team = await create_team(captain, "第二小组")
            stranger_team = await create_team(outsider, "另一个团队")
            assert (await guest.get(f"{_TEAMS}/captained")).status_code == 401
            captained = (await captain.get(f"{_TEAMS}/captained")).json()["items"]
            assert {row["id"] for row in captained} == {team, second_team}
            assert (await learner.get(f"{_TEAMS}/captained")).json()["items"] == []

            private = await create_training(captain, "队内图论练习")
            other_private = await create_training(learner, "个人图论练习")
            private_url = f"{_PRIVATE}/{private['id']}"
            other_private_url = f"{_PRIVATE}/{other_private['id']}"
            team_url = f"{_TEAMS}/{team}"
            catalog_url = f"{team_url}/trainings"
            publish_url = f"{private_url}/publish-team/{team}"
            owner_note = "队长自用笔记"
            item_id = private["items"][0]["id"]
            assert (
                await captain.patch(f"{private_url}/items/{item_id}", json={"note": owner_note})
            ).status_code == 200

            source_ceiling = f"{private_url}/hint-ceiling"
            assert (await captain.put(source_ceiling, json={"max_hint_level": "L1"})).json() == {
                "max_hint_level": "L1"
            }
            confirm = {"confirmed": True}
            assert (await guest.get(catalog_url)).status_code == 401
            assert (await learner.get(catalog_url)).status_code == 403
            assert (await captain.get(catalog_url)).json()["items"] == []
            assert (await captain.post(publish_url, json={"confirmed": False})).status_code == 422
            assert (
                await captain.post(publish_url, json=confirm, headers={"x-csrf-token": ""})
            ).status_code == 403
            assert (
                await captain.post(f"{private_url}/publish-team/{stranger_team}", json=confirm)
            ).status_code == 403
            assert (
                await captain.post(f"{other_private_url}/publish-team/{team}", json=confirm)
            ).status_code == 404

            submitted, retry = await asyncio.gather(
                captain.post(publish_url, json=confirm), captain.post(publish_url, json=confirm)
            )
            assert submitted.status_code == retry.status_code == 201, submitted.text
            shared = submitted.json()
            assert retry.json() == shared
            assert shared["max_hint_level"] == "L1"
            assert (
                await captain.put(source_ceiling, json={"max_hint_level": "L3"})
            ).status_code == 200
            assert (await captain.post(publish_url, json=confirm)).json() == shared
            assert owner_note not in submitted.text
            assert shared["items"][0]["problem_version_id"] == str(version)
            shared_url = f"{catalog_url}/{shared['id']}"
            assert (await guest.get(f"{_OFFICIAL}/{shared['id']}")).status_code == 404
            assert (await captain.post(f"{_OFFICIAL}/{shared['id']}/start")).status_code == 404
            assert (await guest.get(_OFFICIAL)).json()["items"] == []
            assert (await captain.get(private_url)).json().get("official_publication_id") is None
            second = await captain.post(f"{private_url}/publish-team/{second_team}", json=confirm)
            assert second.status_code == 201 and second.json()["id"] != shared["id"]
            assert second.json()["max_hint_level"] == "L3"
            assert (
                await captain.get(f"{_TEAMS}/{second_team}/trainings/{shared['id']}")
            ).status_code == 404

            membership = f"{team_url}/membership"
            moderation = f"{team_url}/members/{learner_id}"
            assert (await learner.put(membership)).status_code == 200
            assert (await learner.get(shared_url)).status_code == 403
            assert (await learner.post(f"{shared_url}/start")).status_code == 403
            assert (await captain.put(moderation, json={"decision": "approve"})).status_code == 200

            visible = await learner.get(shared_url)
            assert visible.json() == shared
            assert visible.headers["cache-control"] == "no-store"
            assert [row["id"] for row in (await learner.get(catalog_url)).json()["items"]] == [
                shared["id"]
            ]
            assert (await outsider.get(shared_url)).status_code == 403
            assert (
                await learner.post(f"{other_private_url}/publish-team/{team}", json=confirm)
            ).status_code == 403
            started, concurrent = await asyncio.gather(
                learner.post(f"{shared_url}/start"), learner.post(f"{shared_url}/start")
            )
            assert started.status_code == concurrent.status_code == 201, started.text
            my_copy = started.json()
            assert concurrent.json()["id"] == my_copy["id"]
            assert my_copy["id"] != private["id"]
            assert my_copy["items"][0]["note"] == ""
            my_copy_url = f"{_PRIVATE}/{my_copy['id']}"
            assert my_copy["max_hint_level"] == my_copy["publication_max_hint_level"] == "L1"
            parameters = {"problem_version_id": str(version), "training_id": my_copy["id"]}
            ceiling_url = "/api/v1/learning/agent/hint-ceiling"
            assert (await learner.get(ceiling_url, params=parameters)).json() == {
                "max_hint_level": "L1"
            }
            assert (await outsider.get(ceiling_url, params=parameters)).status_code == 403
            for requested, effective in (("L0", "L0"), ("L3", "L1")):
                changed = await learner.put(
                    f"{my_copy_url}/hint-ceiling", json={"max_hint_level": requested}
                )
                assert changed.json() == {"max_hint_level": effective}
                assert (await learner.get(ceiling_url, params=parameters)).json() == changed.json()
            db = await connect(postgres_url)
            try:
                async with db.transaction():
                    await db.execute("SET LOCAL ROLE kf_oj_learning_worker")
                    assert not await db.fetchval(
                        "SELECT has_table_privilege(current_user,"
                        "'learning_training_publications','SELECT')"
                    )
                    assert (
                        await db.fetchval(
                            "SELECT public.kf_training_hint_ceiling($1,$2,$3)",
                            UUID(learner_id),
                            UUID(my_copy["id"]),
                            version,
                        )
                        == "L1"
                    )
            finally:
                await db.close()
            assert (await captain.get(my_copy_url)).status_code == 404
            own = await captain.post(f"{shared_url}/start")
            assert own.status_code == 201 and own.json()["id"] != my_copy["id"]

            assert (await captain.put(moderation, json={"decision": "remove"})).status_code == 200
            assert (await learner.get(catalog_url)).status_code == 403
            assert (await learner.get(shared_url)).status_code == 403
            assert (await learner.post(f"{shared_url}/start")).status_code == 403
            assert (await learner.get(my_copy_url)).status_code == 200
            assert (await learner.get(ceiling_url, params=parameters)).json() == {
                "max_hint_level": "L1"
            }

    asyncio.run(exercise())
