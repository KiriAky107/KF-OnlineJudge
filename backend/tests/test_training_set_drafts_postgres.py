"""Save the exact training set reviewed by its owner."""

import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.problems.learning_models import ProblemDifficultyLevel, ProblemLearningMetadata

AGENT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_training_set_revision_and_confirmation_use_its_reviewed_order(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as browser,
            ):
                password = secrets.token_urlsafe(24)
                await app.state.identity.create_account("training_set_learner", password)
                login = await browser.post(
                    "/api/v1/auth/login",
                    json={"username": "training_set_learner", "password": password},
                )
                assert login.status_code == 200
                browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]

                versions = set()
                for slug in ("search-first", "search-second", "search-third"):
                    directory = tmp_path / slug
                    import_fixture(directory, slug=slug)
                    version = await app.state.problems.import_directory(directory)
                    versions.add(str(version))
                    async with app.state.sessions.begin() as db:
                        db.add(
                            ProblemLearningMetadata(
                                problem_version_id=version,
                                tags=["二分查找"],
                                difficulty="easy",
                                revision=1,
                            )
                        )

                catalog = (await browser.get(f"{AGENT}/skills")).json()["versions"]
                skill = next(item for item in catalog if item["purpose"] == "focused_training")
                skill_id = skill["version_id"]
                binding_path = f"{AGENT}/skills/{skill_id}"
                binding = await browser.put(
                    binding_path,
                    json={
                        "enabled": True,
                        "authorized_tools": skill["available_tools"],
                        "parameters": {
                            "tags": ["图论"],
                            "minimum_difficulty": "hard",
                            "maximum_difficulty": "hard",
                            "daily_minutes": 20,
                        },
                    },
                )
                assert binding.status_code == 200, binding.text

                async def session_path():
                    response = await browser.post(
                        f"{AGENT}/sessions", json={"purpose": "focused_training"}
                    )
                    assert response.status_code == 201, response.text
                    return f"{AGENT}/sessions/{response.json()['session_id']}/runs"

                path = await session_path()
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                draft = {
                    "title": "二分入门",
                    "message": "想要两道二分查找入门题",
                    "tags": ["二分查找"],
                    "difficulty": "easy",
                    "count": 2,
                }

                async def send_draft(target, value, request_id=None):
                    return await browser.post(
                        target,
                        json={
                            "request_id": str(request_id or uuid4()),
                            "skill_version_id": skill_id,
                            "training_set": value,
                        },
                    )

                async def complete(value, request_id=None, *, target=None):
                    accepted = await send_draft(target or path, value, request_id)
                    assert accepted.status_code == 202, accepted.text
                    assert accepted.json()["training_request"]["message"] == value["message"]
                    run = f"{AGENT}/runs/{accepted.json()['run_id']}"
                    assert await worker.run_once()
                    result = (await browser.get(run)).json()
                    assert result["state"] == "succeeded", result
                    return run, result

                original_request = uuid4()
                original_path, original = await complete(draft, original_request)
                selected = [item["problem_version_id"] for item in original["result"]["items"]]
                assert len(selected) == 2 and set(selected) < versions
                revision = draft | {
                    "title": "练习二分",
                    "message": "换第二题，改名为练习二分",
                    "revision": {"run_id": original["run_id"], "replace_position": 2},
                }
                other_session = await send_draft(await session_path(), revision)
                assert other_session.status_code == 404
                changed_path, changed = await complete(revision)
                reviewed = [item["problem_version_id"] for item in changed["result"]["items"]]
                (replacement,) = versions.difference(selected)
                assert reviewed == [selected[0], replacement]
                assert changed["result"]["replacement"] == {
                    "position": 2,
                    "previous_title": original["result"]["items"][1]["title"],
                }
                assert (await browser.get(changed_path)).json()["result"]["replacement"] == changed[
                    "result"
                ]["replacement"]
                assert (await browser.get(original_path)).json() == original
                assert changed["training_request"]["message"] == revision["message"]

                rerank = draft | {
                    "count": 1,
                    "message": "这次改成一道题",
                    "previous_run_id": changed["run_id"],
                }
                assert (await send_draft(await session_path(), rerank)).status_code == 404
                resized_path, resized = await complete(rerank)
                assert len(resized["result"]["items"]) == 1
                first_page = await browser.get(f"{resized_path}/conversation", params={"limit": 1})
                assert first_page.status_code == 200, first_page.text
                assert first_page.json()["next_before"] == changed["run_id"]
                (prior,) = first_page.json()["messages"]
                assert prior["question"] == revision["message"]
                assert prior["training_request"]["count"] == 2
                assert prior["training_request"]["tags"] == ["二分查找"]
                earlier = (await browser.get(f"{changed_path}/conversation")).json()
                assert earlier["next_before"] is None
                assert [turn["question"] for turn in earlier["messages"]] == [draft["message"]]

                async def drop_success_response(scope, receive, send):
                    async def deliver(message):
                        if message["type"] == "http.response.start" and message["status"] == 200:
                            raise ConnectionResetError("training confirmation response lost")
                        await send(message)

                    await app(scope, receive, deliver)

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(drop_success_response),
                    base_url=ORIGIN,
                    cookies=browser.cookies,
                    headers=browser.headers,
                ) as disconnected:
                    with pytest.raises(ConnectionResetError, match="training confirmation"):
                        await disconnected.post(f"{changed_path}/training")
                saved_id = (await browser.get(changed_path)).json()["training_id"]
                assert saved_id

                first, second = await asyncio.gather(
                    browser.post(f"{changed_path}/training"),
                    browser.post(f"{changed_path}/training"),
                )
                assert first.status_code == second.status_code == 200, (first.text, second.text)
                assert first.json() == second.json()
                plan = first.json()
                assert plan["id"] == saved_id
                assert plan["title"] == "练习二分"
                assert [item["problem_version_id"] for item in plan["items"]] == reviewed
                assert (await browser.get(changed_path)).json()["training_id"] == plan["id"]
                assert (await browser.get(f"/api/v1/trainings/{plan['id']}")).json() == plan
                training_page = (await browser.get("/api/v1/trainings")).json()
                assert [item["id"] for item in training_page["items"]] == [saved_id]
                saved_skill = next(
                    item
                    for item in (await browser.get(f"{AGENT}/skills")).json()["versions"]
                    if item["version_id"] == skill_id
                )
                assert saved_skill["binding"]["parameters"] == binding.json()["parameters"]

                recent_path = f"{AGENT}/runs?training_drafts=true"
                separate_path, separate = await complete(
                    draft | {"title": "独立入门训练", "count": 1}, target=await session_path()
                )
                confirmed = await browser.post(f"{separate_path}/training")
                assert confirmed.status_code == 200, confirmed.text
                latest = await browser.get(recent_path)
                assert latest.status_code == 200, latest.text
                saved_entry, unsaved_entry = latest.json()["items"]
                assert saved_entry["run_id"] == separate["run_id"]
                assert saved_entry["conversation_title"] == "独立入门训练"
                assert saved_entry["training_id"] == confirmed.json()["id"]
                assert unsaved_entry["run_id"] == resized["run_id"]
                assert "training_id" not in unsaved_entry

                legacy = await browser.post(
                    await session_path(),
                    json={"request_id": str(uuid4()), "skill_version_id": skill_id},
                )
                assert legacy.status_code == 202, legacy.text
                legacy_id = legacy.json()["run_id"]
                discussions = await browser.get(f"{AGENT}/runs?group_chat=true")
                assert discussions.status_code == 200, discussions.text
                assert [item["run_id"] for item in discussions.json()["items"]] == [legacy_id]
                assert (await browser.post(f"{AGENT}/runs/{legacy_id}/cancel")).status_code == 200

                await app.state.identity.create_account("another_learner", password)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as stranger:
                    login = await stranger.post(
                        "/api/v1/auth/login",
                        json={"username": "another_learner", "password": password},
                    )
                    assert login.status_code == 200
                    assert (await stranger.get(recent_path)).json()["items"] == []
                    other = await app.state.identity.authenticate(stranger.cookies["kf_session"])

                rename = draft | {
                    "title": "二分自查",
                    "revision": {"run_id": original["run_id"], "replace_position": None},
                }
                rename_path, renamed = await complete(rename)
                assert [
                    item["problem_version_id"] for item in renamed["result"]["items"]
                ] == selected
                assert (await browser.get(f"{rename_path}/trace")).json()["tool_calls"] == 0

                _, all_problems = await complete(draft | {"count": 3})
                _, unchanged = await complete(
                    draft
                    | {
                        "count": 3,
                        "revision": {"run_id": all_problems["run_id"], "replace_position": 2},
                    }
                )
                assert unchanged["result"]["items"] == all_problems["result"]["items"]
                assert "replacement" not in unchanged["result"]
                assert "当前条件下没有可替换的题目，保留原题单。" in unchanged["result"]["gaps"]

                stale = await send_draft(path, rename | {"title": "旧草案待修改"})
                assert stale.status_code == 202, stale.text
                retired_version = UUID(selected[0])
                async with app.state.sessions.begin() as db:
                    await db.execute(
                        text("UPDATE problems SET enabled=FALSE WHERE active_version_id=:version"),
                        {"version": retired_version},
                    )
                reopened = (await browser.get(original_path)).json()
                assert reopened["result"]["items"] == []
                assert reopened["result"]["gaps"] == [
                    "题库已变更。请调整条件重新生成，或新建训练集。"
                ]
                replay = await send_draft(path, draft, original_request)
                assert replay.status_code == 202 and replay.json() == reopened
                assert (await send_draft(path, rename)).status_code == 409
                assert (await browser.post(f"{original_path}/training")).status_code == 409
                assert await worker.run_once()
                stopped = (await browser.get(f"{AGENT}/runs/{stale.json()['run_id']}")).json()
                assert stopped["failure_code"] == "evidence_unavailable"
                assert stopped["result"] is None
                historical = (await browser.get(f"/api/v1/trainings/{plan['id']}")).json()
                assert [item["problem_version_id"] for item in historical["items"]] == reviewed
                assert not historical["items"][0]["available"]
                async with app.state.sessions.begin() as db:
                    await db.execute(
                        text("UPDATE problems SET enabled=TRUE WHERE active_version_id=:version"),
                        {"version": retired_version},
                    )

                other_topic_dir = tmp_path / "another-topic"
                import_fixture(other_topic_dir, slug="another-topic")
                other_topic = await app.state.problems.import_directory(other_topic_dir)
                candidate_versions = tuple(UUID(value) for value in selected)
                async with app.state.sessions.begin() as db:
                    db.add(
                        ProblemDifficultyLevel(
                            key="custom-search",
                            label="进阶入门",
                            score=1050,
                            color="#36aa80",
                            tier="easy",
                        )
                    )
                    await db.flush()
                    for candidate in candidate_versions:
                        row = await db.get(ProblemLearningMetadata, candidate)
                        row.difficulty_key = "custom-search"
                    db.add(
                        ProblemLearningMetadata(
                            problem_version_id=other_topic,
                            tags=["数组"],
                            difficulty="easy",
                            difficulty_key="custom-search",
                            revision=1,
                        )
                    )
                owner = await app.state.identity.authenticate(browser.cookies["kf_session"])
                operator = await connect(postgres_url)
                try:
                    for actor, version in zip((owner, other), candidate_versions, strict=True):
                        receipt = await app.state.submissions.create(
                            actor,
                            CreateSubmission(
                                problem_version_id=version,
                                language="cpp20",
                                source="int main() { return 0; }",
                                idempotency_key=uuid4(),
                            ),
                        )
                        async with operator.transaction():
                            await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                            await operator.execute(
                                "UPDATE evaluations SET state='finished', verdict='accepted' "
                                "WHERE id=$1",
                                receipt.evaluation_id,
                            )
                finally:
                    await operator.close()
                search = {
                    "title": "站内找题",
                    "message": "找 4 道未通过的二分查找题，难度分 1000 到 1200",
                    "tags": ["二分查找"],
                    "count": 4,
                    "search": {"minimum_score": 1000, "maximum_score": 1200, "only_unsolved": True},
                    "previous_run_id": renamed["run_id"],
                }
                search_path, found = await complete(search)
                (match,) = found["result"]["items"]
                assert match["problem_version_id"] == selected[1]
                assert match["difficulty_score"] == 1050 and match["previously_accepted"] is False
                assert "training_id" not in found
                assert (await browser.post(f"{search_path}/training")).status_code == 409
                group = await browser.get(f"{path.removesuffix('/runs')}/conversation")
                assert group.json()["runs"][-1]["training_request"]["search"] == search["search"]
                updated_plan = (await browser.get(f"/api/v1/trainings/{plan['id']}")).json()
                assert updated_plan["completed_count"] == 1

                queued = await send_draft(path, rename | {"title": "下次练习"})
                assert queued.status_code == 202, queued.text

                revoked = await browser.put(
                    binding_path,
                    json={
                        "enabled": True,
                        "authorized_tools": [],
                        "parameters": binding.json()["parameters"],
                    },
                )
                assert revoked.status_code == 200, revoked.text
                assert (await send_draft(path, rename)).status_code == 404
                assert (await browser.post(f"{rename_path}/training")).status_code == 404
                assert await worker.run_once()
                denied_run = (await browser.get(f"{AGENT}/runs/{queued.json()['run_id']}")).json()
                assert denied_run["state"] == "failed"
                assert denied_run["failure_code"] == "authorization_unavailable"
                assert denied_run["result"] is None
                assert (await browser.get(f"/api/v1/trainings/{plan['id']}")).json() == updated_plan
        finally:
            await worker_engine.dispose()

    asyncio.run(exercise())
