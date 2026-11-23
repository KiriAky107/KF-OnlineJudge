"""Verify practice dates and recommendation order using synthetic, unexecuted evaluations."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata
from kf_oj.modules.submissions.public import learning_records

ROOT = "/api/v1"
AGENT = f"{ROOT}/learning/agent"
ORIGIN = "http://localhost:5173"
TOPIC = "动态规划"


def test_saved_assistant_plan_does_not_report_a_future_ac(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        worker_engine = create_async_engine(postgres_url)
        seed_engine = create_async_engine(postgres_url)

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
                ) as owner,
            ):
                password = secrets.token_urlsafe(24)
                actor = await app.state.identity.create_account("plan_cutoff_owner", password)
                login = await owner.post(
                    f"{ROOT}/auth/login",
                    json={"username": "plan_cutoff_owner", "password": password},
                )
                assert login.status_code == 200
                owner.headers["x-csrf-token"] = owner.cookies["kf_csrf"]
                version = await app.state.problems.import_directory(tmp_path / "import")
                async with app.state.sessions.begin() as db:
                    db.add(
                        ProblemLearningMetadata(
                            problem_version_id=version, tags=[TOPIC], difficulty="easy", revision=1
                        )
                    )
                future = await app.state.submissions.create(
                    actor,
                    CreateSubmission(
                        problem_version_id=version,
                        language="c17",
                        idempotency_key=uuid4(),
                        source="synthetic fixture never executed",
                    ),
                )
                async with seed_engine.begin() as db:
                    await db.execute(
                        text("UPDATE submissions SET created_at=:created WHERE id=:id"),
                        {
                            "created": datetime.now(UTC) + timedelta(days=2),
                            "id": future.submission_id,
                        },
                    )
                    await db.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                    await db.execute(
                        text(
                            "UPDATE evaluations SET state='finished', verdict='accepted' "
                            "WHERE id=:id"
                        ),
                        {"id": future.evaluation_id},
                    )

                catalog = (await owner.get(f"{AGENT}/skills")).json()["versions"]
                skill = next(item for item in catalog if item["purpose"] == "focused_training")
                skill_id = skill["version_id"]
                saved = await owner.put(
                    f"{AGENT}/skills/{skill_id}",
                    json={
                        "enabled": True,
                        "parameters": {"tags": [TOPIC], "daily_minutes": 20},
                        "authorized_tools": skill["available_tools"],
                    },
                )
                assert saved.status_code == 200
                session = await owner.post(
                    f"{AGENT}/sessions", json={"purpose": "focused_training"}
                )
                assert session.status_code == 201
                queued = await owner.post(
                    f"{AGENT}/sessions/{session.json()['session_id']}/runs",
                    json={"skill_version_id": skill_id, "request_id": str(uuid4())},
                )
                assert queued.status_code == 202
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                assert await worker.run_once()
                confirmation = await owner.post(f"{AGENT}/runs/{queued.json()['run_id']}/training")
                assert confirmation.status_code == 200, confirmation.text
                plan = confirmation.json()
                (item,) = plan["items"]
                assert item["problem_version_id"] == str(version)
                assert not item["previously_accepted"] and item["progress"] == "not_started"
                assert item["progress_evidence"] == [] and plan["completed_count"] == 0
                assert (await owner.get(f"{ROOT}/trainings")).json()["items"][0][
                    "completed_count"
                ] == 0

                async with seed_engine.begin() as db:
                    moved = await db.scalar(
                        text(
                            "UPDATE submissions SET created_at=clock_timestamp() "
                            "WHERE id=:id RETURNING created_at"
                        ),
                        {"id": future.submission_id},
                    )
                assert moved >= datetime.fromisoformat(plan["created_at"])
                read_back = await owner.get(f"{ROOT}/trainings/{plan['id']}")
                assert read_back.status_code == 200
                actual = read_back.json()
                assert actual["completed_count"] == 1
                (completed,) = actual["items"]
                assert completed["progress"] == "completed" and not completed["previously_accepted"]
                assert completed["progress_evidence"][0]["evaluation_id"] == str(
                    future.evaluation_id
                )
        finally:
            await worker_engine.dispose()
            await seed_engine.dispose()

    asyncio.run(exercise())


def test_recommendations_space_recent_practice_and_keep_fixed_dates(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, platform_engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: platform_engine)
        worker_engine = create_async_engine(postgres_url)
        seed_engine = create_async_engine(postgres_url)
        now = datetime.now(UTC)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_worker(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
            )

        async def login(browser, name):
            password = secrets.token_urlsafe(24)
            principal = await app.state.identity.create_account(name, password)
            result = await browser.post(
                f"{ROOT}/auth/login", json={"username": name, "password": password}
            )
            assert result.status_code == 200, result.text
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
            return principal

        async def problem(slug, difficulty, tags):
            directory = tmp_path / slug
            import_fixture(directory, slug=slug)
            version = await app.state.problems.import_directory(directory)
            async with app.state.sessions.begin() as db:
                db.add(
                    ProblemLearningMetadata(
                        problem_version_id=version, tags=tags, difficulty=difficulty, revision=1
                    )
                )
            return version

        async def grade(principal, version, days, verdict):
            receipt = await app.state.submissions.create(
                principal,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    idempotency_key=uuid4(),
                    source="synthetic fixture never executed",
                ),
            )
            created = now - timedelta(days=days)
            async with seed_engine.begin() as db:
                await db.execute(
                    text("UPDATE submissions SET created_at=:created WHERE id=:id"),
                    {"created": created, "id": receipt.submission_id},
                )
                await db.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                await db.execute(
                    text(
                        "UPDATE evaluations SET state='finished', verdict=:verdict, "
                        "updated_at=:finished WHERE id=:id"
                    ),
                    {
                        "verdict": verdict,
                        "id": receipt.evaluation_id,
                        "finished": created + timedelta(seconds=1),
                    },
                )
            return created

        async def recommend(browser):
            response = await browser.post(
                f"{ROOT}/learning/recommendations", json={"tags": [TOPIC], "count": 5}
            )
            assert response.status_code == 200, response.text
            return response.json()

        try:
            async with app.router.lifespan_context(app), client() as owner, client() as other:
                principal = await login(owner, "timing_owner")
                outsider = await login(other, "timing_outsider")
                due = await problem("review-due", "medium", [TOPIC])
                fresh = await problem("first-practice", "easy", [TOPIC])
                later = await problem("review-later", "medium", [TOPIC])
                repeated = await problem("recent-submission", "easy", [TOPIC])
                planned = await problem("recent-plan", "easy", [TOPIC, "只有此题"])
                last_pass = await grade(principal, due, 120, "accepted")
                await grade(principal, later, 10, "accepted")
                await grade(principal, repeated, 1, "wrong_answer")
                await grade(outsider, fresh, 40, "accepted")
                # A future fixture cannot become advance knowledge during the current request.
                await grade(principal, fresh, -2, "accepted")
                async with app.state.sessions() as db:
                    visible = await learning_records(
                        db, principal, since=now - timedelta(days=90), as_of=now, limit=1
                    )
                assert visible.truncated
                (latest,) = visible.records
                assert latest.version_id == repeated and latest.verdict == "wrong_answer"
                made = await owner.post(
                    f"{ROOT}/trainings",
                    json={"tags": ["只有此题"], "count": 1, "idempotency_key": str(uuid4())},
                )
                assert made.status_code == 201, made.text
                (previous,) = made.json()["items"]
                assert previous["problem_version_id"] == str(planned) and previous["timing"] is None

                result = await recommend(owner)
                assert result["rule_version"] == "learning-rules-v3"
                versions = [item["problem_version_id"] for item in result["items"]]
                assert versions[:3] == list(map(str, (due, fresh, later)))
                assert set(versions[3:]) == {str(repeated), str(planned)}
                items = {item["problem_version_id"]: item for item in result["items"]}
                assert items[str(fresh)]["timing"] is None
                assert items[str(later)]["timing"]["status"] == "review_later"
                assert items[str(repeated)]["timing"]["status"] == "recent_attempt"
                assert items[str(planned)]["timing"]["status"] == "recent_plan"
                assert items[str(planned)]["timing"]["last_planned_at"] == made.json()["created_at"]
                timing = items[str(due)]["timing"]
                assert timing["status"] == "review_due"
                assert datetime.fromisoformat(timing["last_accepted_at"]) == last_pass
                assert datetime.fromisoformat(timing["review_due_at"]) == last_pass + timedelta(
                    days=14
                )
                foreign = await recommend(other)
                assert foreign["items"][0]["problem_version_id"] == str(fresh)
                assert all(item["timing"] is None for item in foreign["items"][1:])

                catalog = (await owner.get(f"{AGENT}/skills")).json()["versions"]
                skill = next(entry for entry in catalog if entry["purpose"] == "focused_training")
                version = skill["version_id"]
                settings = await owner.put(
                    f"{AGENT}/skills/{version}",
                    json={
                        "enabled": True,
                        "authorized_tools": skill["available_tools"],
                        "parameters": {
                            "tags": [TOPIC],
                            "daily_minutes": 20,
                            "maximum_difficulty": "medium",
                        },
                    },
                )
                assert settings.status_code == 200, settings.text
                session = await owner.post(
                    f"{AGENT}/sessions", json={"purpose": "focused_training"}
                )
                assert session.status_code == 201, session.text
                queued = await owner.post(
                    f"{AGENT}/sessions/{session.json()['session_id']}/runs",
                    json={"skill_version_id": version, "request_id": str(uuid4())},
                )
                assert queued.status_code == 202, queued.text
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))
                assert await worker.run_once()
                async with worker_engine.connect() as db:
                    assert not await db.scalar(
                        text("SELECT has_table_privilege(current_user,'learning_plans','SELECT')")
                    )
                run = f"{AGENT}/runs/{queued.json()['run_id']}"
                finished = (await owner.get(run)).json()
                assert finished["state"] == "succeeded", finished
                (selected,) = finished["result"]["items"]
                assert selected["problem_version_id"] == str(due)
                assert selected["timing"] == timing
                saved = await owner.post(f"{run}/training")
                assert saved.status_code == 200, saved.text
                assert saved.json()["rule_version"] == "agent-rules-v3"
                assert saved.json()["items"][0]["timing"] == timing

                review_priority = await problem("weighted-review", "medium", ["加权算法", "复习题"])
                gap_priority = await problem("weighted-gap", "easy", ["加权算法", "区间"])
                gap_evidence = await problem("weighted-gap-evidence", "easy", ["区间"])
                feedback_problem = await problem(
                    "weighted-feedback", "easy", ["复习题", "自评采集"]
                )
                await grade(principal, review_priority, 20, "accepted")
                await grade(principal, gap_evidence, 1, "wrong_answer")
                previous_plan = await owner.post(
                    f"{ROOT}/trainings",
                    json={
                        "tags": ["自评采集"],
                        "count": 1,
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert previous_plan.status_code == 201, previous_plan.text
                (source_row,) = previous_plan.json()["items"]
                assert source_row["problem_version_id"] == str(feedback_problem)
                feedback = await owner.put(
                    f"{ROOT}/trainings/{previous_plan.json()['id']}/items/{source_row['id']}/feedback",
                    json={"rating": "too_easy"},
                )
                assert feedback.status_code == 200

                selection = {"tags": ["加权算法"], "count": 1}
                endpoint = f"{ROOT}/learning/recommendations"
                ordinary = await owner.post(endpoint, json=selection)
                assert ordinary.json()["items"][0]["problem_version_id"] == str(review_priority)
                for profile, expected in (
                    ("feedback_first", review_priority),
                    ("unresolved_first", gap_priority),
                ):
                    recommendation = await owner.post(
                        endpoint, json=selection | {"ranking_profile": profile}
                    )
                    assert recommendation.status_code == 200, recommendation.text
                    assert recommendation.json()["rule_version"] == "learning-rules-v6"
                    assert recommendation.json()["parameters"]["ranking_profile"] == profile
                    assert recommendation.json()["items"][0]["problem_version_id"] == str(expected)
                filtered = await owner.post(
                    endpoint,
                    json=selection
                    | {"difficulty": "medium", "ranking_profile": "unresolved_first"},
                )
                assert filtered.json()["items"][0]["problem_version_id"] == str(review_priority)

                settings = await owner.put(
                    f"{AGENT}/skills/{version}",
                    json={
                        "enabled": True,
                        "authorized_tools": skill["available_tools"],
                        "parameters": {
                            "tags": ["加权算法"],
                            "daily_minutes": 20,
                            "maximum_difficulty": "medium",
                            "ranking_profile": "unresolved_first",
                        },
                    },
                )
                assert settings.status_code == 200, settings.text
                queued = await owner.post(
                    f"{AGENT}/sessions/{session.json()['session_id']}/runs",
                    json={"skill_version_id": version, "request_id": str(uuid4())},
                )
                assert queued.status_code == 202, queued.text
                assert await worker.run_once()
                weighted_run = f"{AGENT}/runs/{queued.json()['run_id']}"
                weighted = (await owner.get(weighted_run)).json()
                assert weighted["state"] == "succeeded", weighted
                assert weighted["result"]["ranking_profile"] == "unresolved_first"
                assert weighted["result"]["items"][0]["problem_version_id"] == str(gap_priority)
                trace = (await owner.get(weighted_run + "/trace")).json()
                assert trace["finished_tool_calls"] == 1
                weighted_saved = await owner.post(weighted_run + "/training")
                assert weighted_saved.status_code == 200, weighted_saved.text
                assert weighted_saved.json()["rule_version"] == "agent-rules-v6"
                assert weighted_saved.json()["parameters"]["ranking_profile"] == "unresolved_first"
                successor = await owner.post(
                    f"{ROOT}/trainings/{weighted_saved.json()['id']}/follow-ups",
                    json={"idempotency_key": str(uuid4())},
                )
                assert successor.status_code == 201, successor.text
                assert successor.json()["parameters"]["ranking_profile"] == "unresolved_first"
        finally:
            await worker_engine.dispose()
            await seed_engine.dispose()

    asyncio.run(exercise())
