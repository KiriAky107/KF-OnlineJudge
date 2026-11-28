"""Official publication and account-owned practice through real platform sessions."""

import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
from conftest import connect
from test_authority_operations import authority
from test_submission_create import setup_app

_ORIGIN = "http://localhost:5173"
_PRIVATE = "/api/v1/trainings"
_OFFICIAL = "/api/v1/official-trainings"


def test_official_training_preserves_public_order_and_separates_account_state(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=_ORIGIN,
                headers={"origin": _ORIGIN},
            )

        async def setup_browser(browser, username):
            password = secrets.token_urlsafe(24)
            await app.state.identity.create_account(username, password)
            if username == "official_publisher":
                assert (await authority(db, username, "bootstrap"))["changed"]
            response = await browser.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]

        async def private_plan(browser, title):
            result = await browser.post(
                _PRIVATE, json={"count": 1, "title": title, "idempotency_key": str(uuid4())}
            )
            assert result.status_code == 201, result.text
            return result.json()

        db = await connect(postgres_url)
        try:
            async with (
                app.router.lifespan_context(app),
                client() as owner,
                client() as learner,
                client() as other,
                client() as guest,
            ):
                version = await app.state.problems.import_directory(tmp_path / "import")
                await setup_browser(owner, "official_publisher")
                await setup_browser(learner, "official_learner")
                await setup_browser(other, "official_other")
                private = await private_plan(owner, "图论入门")
                own_learner = await private_plan(learner, "本人练习")
                origin = f"{_PRIVATE}/{private['id']}"
                foreign_origin = f"{_PRIVATE}/{own_learner['id']}"
                publish = f"{origin}/publish-official"
                assert (await guest.get(_OFFICIAL)).json()["items"] == []
                assert (await learner.get(origin)).status_code == 404
                assert (
                    await learner.post(
                        f"{foreign_origin}/publish-official", json={"confirmed": True}
                    )
                ).status_code == 403
                assert (
                    await owner.post(f"{foreign_origin}/publish-official", json={"confirmed": True})
                ).status_code == 404
                assert (await owner.post(publish, json={"confirmed": 1})).status_code == 422
                assert (
                    await owner.post(
                        publish, json={"confirmed": True}, headers={"x-csrf-token": ""}
                    )
                ).status_code == 403
                original_item = private["items"][0]
                note = "原作者的私密笔记"
                assert (
                    await owner.patch(f"{origin}/items/{original_item['id']}", json={"note": note})
                ).status_code == 200
                assert (
                    await owner.put(
                        f"{origin}/items/{original_item['id']}/feedback",
                        json={"rating": "too_easy"},
                    )
                ).status_code == 200
                assert (
                    await owner.put(f"{origin}/hint-ceiling", json={"max_hint_level": "L2"})
                ).status_code == 200
                first, repeated = await asyncio.gather(
                    owner.post(publish, json={"confirmed": True}),
                    owner.post(publish, json={"confirmed": True}),
                )
                assert first.status_code == repeated.status_code == 201, first.text
                public = first.json()
                assert repeated.json() == public
                assert public["id"] != private["id"]
                assert (await owner.get(origin)).json()["official_publication_id"] == public["id"]
                assert set(public) == {
                    "id",
                    "title",
                    "item_count",
                    "created_at",
                    "items",
                    "max_hint_level",
                }
                assert public["max_hint_level"] == "L2"
                assert set(public["items"][0]) == {
                    "position",
                    "problem_version_id",
                    "title",
                    "difficulty",
                }
                assert public["items"][0]["problem_version_id"] == str(version)
                public_path = f"{_OFFICIAL}/{public['id']}"
                assert (await guest.get(public_path)).json() == public
                assert note not in first.text
                assert (await guest.post(f"{public_path}/start")).status_code == 401

                newer = await app.state.problems.import_directory(tmp_path / "import")
                assert newer != version
                assert (await guest.get(public_path)).json() == public
                mine, retry = await asyncio.gather(
                    learner.post(f"{public_path}/start"), learner.post(f"{public_path}/start")
                )
                assert mine.status_code == retry.status_code == 201, mine.text
                fork = mine.json()
                assert retry.json()["id"] == fork["id"]
                companion = (await other.post(f"{public_path}/start")).json()
                assert len({fork["id"], companion["id"], private["id"]}) == 3
                for copy in (fork, companion):
                    assert copy["max_hint_level"] == copy["publication_max_hint_level"] == "L2"
                    item = copy["items"][0]
                    assert item["note"] == "" and item.get("feedback") is None
                    assert item["problem_version_id"] == str(version)
                private_path = f"{_PRIVATE}/{fork['id']}"
                assert (await other.get(private_path)).status_code == 404
                item = fork["items"][0]["id"]
                assert (
                    await learner.patch(
                        f"{private_path}/items/{item}", json={"note": "另一份个人笔记"}
                    )
                ).status_code == 200
                assert (
                    await learner.put(
                        f"{private_path}/items/{item}/feedback", json={"rating": "too_hard"}
                    )
                ).status_code == 200
                submitted = await learner.post(
                    "/api/v1/submissions",
                    json={
                        "problem_version_id": str(version),
                        "language": "c17",
                        "source": "int main(void) { return 0; }",
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert submitted.status_code == 201
                await db.execute(
                    "UPDATE evaluations SET state='finished',verdict='accepted',"
                    "updated_at=clock_timestamp() WHERE id=$1",
                    UUID(submitted.json()["evaluation_id"]),
                )
                advanced = (await learner.get(private_path)).json()
                assert advanced["completed_count"] == 1
                unaffected = (await other.get(f"{_PRIVATE}/{companion['id']}")).json()["items"][0]
                assert unaffected["note"] == "" and unaffected.get("feedback") is None
                assert unaffected["progress"] == "not_started"
                assert (await guest.get(public_path)).json() == public

                await db.execute(
                    "UPDATE problems SET enabled=false WHERE active_version_id=$1", newer
                )
                redacted = (await guest.get(public_path)).json()
                assert redacted["items"][0]["title"] is None
                assert redacted["items"][0]["problem_version_id"] is None
                assert (await owner.post(f"{public_path}/start")).status_code == 409
                assert (await learner.post(f"{public_path}/start")).json()["id"] == fork["id"]
                assert not await db.fetchval(
                    "SELECT has_table_privilege('kf_oj_learning_worker', "
                    "'learning_training_publication_items','SELECT')"
                )
        finally:
            await db.close()

    asyncio.run(exercise())
