"""Exercise reviewed publication against PostgreSQL using real Cookie authority."""

import asyncio
import hashlib
import secrets
from uuid import uuid4

import httpx
from conftest import connect
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"
REVIEWS = "/api/v1/admin/skill-reviews"


def test_only_reviewed_skill_revision_is_discoverable(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        migration_admin = await connect(postgres_url)

        def http():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        async def login(client, username, *, admin=False):
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account(username, password)
            if admin:
                await set_administrator(migration_admin, account.account_id, True)
            response = await client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200, response.text
            client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

        async def draft(client, document):
            response = await client.post(
                f"{ROOT}/skill-drafts", json={"request_id": str(uuid4()), "document": document}
            )
            assert response.status_code == 201, response.text
            return response.json()

        try:
            async with (
                app.router.lifespan_context(app),
                http() as author,
                http() as reader,
                http() as reviewer,
            ):
                await login(author, "skill_author")
                await login(reader, "skill_reader")
                await login(reviewer, "skill_reviewer", admin=True)
                library = f"{ROOT}/skill-library"
                original = {
                    "purpose": "submission_review",
                    "title": "先看证据 %v1",
                    "description": "从可验证的提交结果入手",
                    "method": "对比同题提交和判定。",
                    "license": "CC BY 4.0",
                    "attribution": "原作者署名",
                    "references": [
                        {
                            "path": "references/review-notes.md",
                            "content": "先记录可以重现的公开样例。\n" r"再检查文本中的 \u0000。",
                        },
                    ],
                }
                notes = original["references"][0]
                digest = hashlib.sha256(notes["content"].encode()).hexdigest()
                for invalid in (
                    [notes | {"path": "notes.md"}],
                    [notes | {"path": "references/review-notes.pdf"}],
                    [notes, notes],
                    [notes | {"sha256": "0" * 64}],
                    [notes | {"content": "含空字符的文本\x00"}],
                ):
                    rejected = await author.post(
                        f"{ROOT}/skill-drafts",
                        json={
                            "request_id": str(uuid4()),
                            "document": original | {"references": invalid},
                        },
                    )
                    assert rejected.status_code == 422, rejected.text
                entry = await draft(author, original)
                exported = entry["document"]
                assert exported["references"] == [notes | {"sha256": digest}]
                assert (await draft(author, exported))["content_digest"] == entry["content_digest"]
                path = f"{ROOT}/skill-drafts/{entry['draft_id']}"
                releases = f"{path}/releases"
                share = {"revision": entry["revision"], "confirmed_public": True}
                assert (
                    await author.post(releases, json=share | {"confirmed_public": False})
                ).status_code == 422
                assert (
                    await author.post(releases, json=share, headers={"x-csrf-token": ""})
                ).status_code == 403
                assert (await reader.get(releases)).status_code == 404
                assert (await reader.post(releases, json=share)).status_code == 404
                assert (await reader.get(REVIEWS)).status_code == 403
                sent, twin = await asyncio.gather(
                    *(author.post(releases, json=share) for _ in range(2))
                )
                assert sent.status_code == twin.status_code == 201
                pending = sent.json()
                assert twin.json() == pending
                version = pending["version_id"]
                review_path = f"{REVIEWS}/{version}"
                detail = f"{ROOT}/skill-library/{version}"
                favorite = f"{library}/{entry['draft_id']}/favorite"
                save_favorite = {"favorited": True}
                assert (await reader.put(favorite, json=save_favorite)).status_code == 404
                assert pending["state"] == "pending"
                assert (await reader.get(detail)).status_code == 404
                copy = {"source_version_id": version, "request_id": str(uuid4())}
                assert (await reader.post(f"{ROOT}/skill-drafts", json=copy)).status_code == 404
                assert (await reader.get(f"{ROOT}/skill-library")).json() == {
                    "items": [],
                    "truncated": False,
                }
                assert (
                    await author.post(review_path, json={"action": "publish"})
                ).status_code == 403

                edited = await author.put(
                    path,
                    json={
                        "revision": entry["revision"],
                        "document": original
                        | {
                            "title": "未审核的新修订",
                            "references": [notes | {"content": "编辑后的私有文本"}],
                        },
                    },
                )
                assert edited.status_code == 200, edited.text
                assert edited.json()["document"]["references"] != exported["references"]
                assert (await reviewer.get(REVIEWS)).json()["items"] == [pending]
                assert (
                    await reviewer.post(review_path, json={"action": "reject"})
                ).status_code == 422
                decision = {"action": "publish", "note": "仅供作者和审核方查看"}
                approved = await reviewer.post(review_path, json=decision)
                assert approved.status_code == 200, approved.text
                assert (await reviewer.post(review_path, json=decision)).json() == approved.json()
                assert (await reviewer.get(REVIEWS)).json()["items"] == []
                assert (await reviewer.get(REVIEWS, params={"state": "published"})).json()[
                    "items"
                ] == [approved.json()]
                assert (await author.get(releases)).json()["items"] == [approved.json()]
                response = await reader.get(detail)
                assert (
                    response.status_code == 200 and response.headers["cache-control"] == "no-store"
                )
                public = response.json()
                assert public["document"] == entry["document"]
                assert public["content_digest"] == entry["content_digest"]
                assert "review_note" not in public and "owner_id" not in public
                copies = await asyncio.gather(
                    *(reader.post(f"{ROOT}/skill-drafts", json=copy) for _ in range(2))
                )
                assert all(result.status_code == 201 for result in copies)
                fork = copies[0].json()
                assert copies[1].json() == fork
                assert fork["source_version_id"] == version
                assert fork["document"]["title"] == entry["document"]["title"]
                assert fork["document"]["method"] == entry["document"]["method"]
                assert fork["document"]["license"] == entry["document"]["license"]
                assert fork["document"]["references"] == exported["references"]
                assert fork["document"]["attribution"] == (
                    f"原作者署名 · skill_author / v1 ({version})"
                )
                private_copy = f"{ROOT}/skill-drafts/{fork['draft_id']}"
                assert (await author.get(private_copy)).status_code == 404
                edited_fork = await reader.put(
                    private_copy,
                    json={
                        "revision": fork["revision"],
                        "document": fork["document"] | {"title": "读者的私有变体"},
                    },
                )
                assert edited_fork.status_code == 200
                assert (
                    await reader.post(f"{ROOT}/skill-drafts", json=copy)
                ).json() == edited_fork.json()
                assert (await reader.get(detail)).json() == public
                shared_fork = await reader.post(
                    f"{private_copy}/releases",
                    json={"revision": edited_fork.json()["revision"], "confirmed_public": True},
                )
                assert shared_fork.status_code == 201
                assert shared_fork.json()["based_on_version_id"] == version
                assert (
                    await reader.get(
                        library, params={"search": "%v1", "purpose": "submission_review"}
                    )
                ).json()["items"] == [public]
                for filters in ({"search": "%_"}, {"search": "未审核"}, {"purpose": "guided_help"}):
                    assert (await reader.get(library, params=filters)).json()["items"] == []
                assert (await reader.get(library, params={"search": "\x00"})).status_code == 422
                assert public["favorited"] is False
                forbidden = await reader.put(
                    favorite, json=save_favorite, headers={"x-csrf-token": ""}
                )
                assert forbidden.status_code == 403
                saved = await asyncio.gather(
                    *(reader.put(favorite, json=save_favorite) for _ in range(2))
                )
                assert all(result.json() == save_favorite for result in saved)
                only_favorites = {"favorites_only": True}
                saved_items = (await reader.get(library, params=only_favorites)).json()["items"]
                assert saved_items == [public | {"favorited": True}]
                assert (await author.get(library, params=only_favorites)).json()["items"] == []
                assert (await author.get(detail)).json()["favorited"] is False
                assert (await reader.get(detail)).json()["favorited"] is True

                unavailable = await draft(
                    author, original | {"parameters": {"problem_ids": [str(uuid4())]}}
                )
                assert (
                    await author.post(
                        f"{ROOT}/skill-drafts/{unavailable['draft_id']}/releases", json=share
                    )
                ).status_code == 422
                revision_2 = await author.post(
                    releases, json=share | {"revision": edited.json()["revision"]}
                )
                assert revision_2.status_code == 201
                withdraw = f"{ROOT}/skill-versions/{revision_2.json()['version_id']}/withdraw"
                assert (await reader.post(withdraw)).status_code == 404
                aborted = await author.post(withdraw)
                assert aborted.status_code == 200 and aborted.json()["state"] == "withdrawn"
                assert (await author.post(withdraw)).json() == aborted.json()
                assert (
                    await author.post(f"{ROOT}/skill-versions/{version}/withdraw")
                ).status_code == 409
                newer_draft = await author.put(
                    path,
                    json={
                        "revision": edited.json()["revision"],
                        "document": original | {"title": "再次送审的版本"},
                    },
                )
                assert newer_draft.status_code == 200
                newer = await author.post(
                    releases, json=share | {"revision": newer_draft.json()["revision"]}
                )
                assert newer.status_code == 201
                review_newer = await reviewer.post(
                    f"{REVIEWS}/{newer.json()['version_id']}", json={"action": "publish"}
                )
                assert review_newer.status_code == 200
                favorites = (await reader.get(library, params=only_favorites)).json()["items"]
                assert {item["version_id"] for item in favorites} == {
                    version,
                    newer.json()["version_id"],
                }
                assert (await reader.put(favorite, json={"favorited": False})).status_code == 200
                assert (await reader.get(library, params=only_favorites)).json()["items"] == []
                assert (await reader.get(detail)).json()["favorited"] is False
                assert not await migration_admin.fetchval(
                    "SELECT has_column_privilege("
                    "'kf_oj_platform', 'skills_publications', 'document', 'UPDATE')"
                )
                assert not await migration_admin.fetchval(
                    "SELECT has_table_privilege("
                    "'kf_oj_learning_worker', 'skills_publications', 'SELECT')"
                )
        finally:
            await migration_admin.close()

    asyncio.run(exercise())
