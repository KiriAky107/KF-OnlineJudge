"""Exercise real aggregate and settlement data without running any submissions."""

import asyncio
import secrets
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from conftest import connect
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.contests.rating import rating_deltas


def test_cf_style_delta_reference_cases():
    assert rating_deltas([1400, 1400], [1, 2]) == [65, -67]
    assert rating_deltas([1400, 1400, 1400], [1, 2, 2]) == [65, -34, -34]


def test_owner_activity_final_standings_and_custom_difficulty(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            passwords = {name: secrets.token_urlsafe(24) for name in ("work_admin", "work_member")}
            accounts = {
                name: await app.state.identity.create_account(name, password)
                for name, password in passwords.items()
            }
            zone = ZoneInfo("Asia/Shanghai")
            midnight = datetime.combine(
                datetime.now(zone).date() - timedelta(days=1), time.min, zone
            )
            version = await app.state.problems.import_directory(tmp_path / "import")
            created = [
                await app.state.submissions.create(
                    accounts[name],
                    CreateSubmission(
                        problem_version_id=version,
                        language="c17",
                        idempotency_key=uuid4(),
                        source="fixture only",
                    ),
                )
                for name in ("work_admin", "work_admin", "work_member", "work_admin")
            ]
            conn = await connect(postgres_url)
            try:
                await set_administrator(conn, accounts["work_admin"].account_id, True)
                await conn.executemany(
                    "UPDATE submissions SET created_at=$1 WHERE id=$2",
                    list(
                        zip(
                            (
                                midnight - timedelta(seconds=1),
                                midnight,
                                midnight,
                                midnight - timedelta(days=366),
                            ),
                            (item.submission_id for item in created),
                            strict=True,
                        )
                    ),
                )
            finally:
                await conn.close()
            origin = "http://localhost:8000"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=origin
            ) as client:
                activity_path = "/api/v1/submissions/activity"
                assert (await client.get(activity_path)).status_code == 401

                async def login(name):
                    response = await client.post(
                        "/api/v1/auth/login",
                        json={"username": name, "password": passwords[name]},
                        headers={"Origin": origin},
                    )
                    assert response.status_code == 200
                    return {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}

                now = datetime.now(UTC)
                contest = {
                    "key": "round-one",
                    "title": "第一场正式赛",
                    "ended_at": (now - timedelta(hours=2)).isoformat(),
                    "standings": [
                        {"username": "work_admin", "rank": 1},
                        {"username": "work_member", "rank": 2},
                    ],
                }
                settlement = "/api/v1/admin/contest-settlements"
                rating = "/api/v1/contests/my-rating"
                difficulties = "/api/v1/problems/difficulty-levels"
                custom = "/api/v1/admin/problem-difficulty-levels/beginner_plus"
                level = {"label": "进阶入门", "score": 1050, "color": "#36aa80", "tier": "easy"}
                headers = await login("work_member")
                assert (
                    await client.post(settlement, json=contest, headers=headers)
                ).status_code == 403
                assert (await client.put(custom, json=level, headers=headers)).status_code == 403
                assert (await client.get(rating)).json()["rating"] is None
                response = await client.get(activity_path)
                assert response.headers["Cache-Control"] == "no-store"
                assert response.json()["days"] == [{"day": midnight.date().isoformat(), "count": 1}]

                headers = await login("work_admin")
                response = await client.get(activity_path)
                assert response.json()["days"] == [
                    {"day": (midnight.date() - timedelta(days=1)).isoformat(), "count": 1},
                    {"day": midnight.date().isoformat(), "count": 1},
                ]
                assert response.json()["time_zone"] == "Asia/Shanghai"

                response = await client.post(settlement, json=contest, headers=headers)
                assert response.status_code == 200, response.text
                retry = await client.post(
                    settlement,
                    json=contest | {"standings": list(reversed(contest["standings"]))},
                    headers=headers,
                )
                assert retry.json() == response.json()
                changed = await client.post(
                    settlement, json=contest | {"title": "different"}, headers=headers
                )
                assert changed.status_code == 409
                own_rating = (await client.get(rating)).json()
                assert own_rating["rating"] == own_rating["highest_rating"] == 565
                assert own_rating["rated_contests"] == 1 and own_rating["history"][0]["rank"] == 1

                metadata = f"/api/v1/admin/problem-versions/{version}/learning-metadata"
                assert (await client.get(difficulties)).json()[0]["key"] == "easy"
                response = await client.put(custom, json=level, headers=headers)
                assert response.status_code == 200, response.text
                assert (
                    await client.put(custom, json=level | {"tier": "hard"}, headers=headers)
                ).status_code == 422
                response = await client.put(
                    metadata,
                    headers=headers,
                    json={
                        "tags": ["数组"],
                        "difficulty": "easy",
                        "difficulty_key": "beginner_plus",
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["difficulty_key"] == "beginner_plus"
                response = await client.get(f"/api/v1/problems/versions/{version}")
                assert response.json()["difficulty_level"] == level | {"key": "beginner_plus"}
                page = await client.get(
                    "/api/v1/problems", params={"difficulty_key": "beginner_plus"}
                )
                assert [item["version_id"] for item in page.json()["items"]] == [str(version)]
                assert (
                    await client.get("/api/v1/problems", params={"difficulty_key": "easy"})
                ).json()["items"] == []
                assert (await client.get("/api/v1/problems", params={"difficulty": "easy"})).json()[
                    "items"
                ]
                next_contest = {
                    "key": "round-two",
                    "title": "第二场正式赛",
                    "ended_at": (now - timedelta(hours=1)).isoformat(),
                    "standings": [
                        {"username": "work_admin", "rank": 2},
                        {"username": "work_member", "rank": 1},
                    ],
                }
                response = await client.post(settlement, json=next_contest, headers=headers)
                assert response.status_code == 200, response.text
                conflict = await client.post(
                    settlement, json=contest | {"key": "older-round"}, headers=headers
                )
                assert conflict.status_code == 409
                next_rating = (await client.get(rating)).json()
                assert next_rating["rated_contests"] == 2 and next_rating["rating"] == 839
                assert next_rating["history"][0]["old_rating"] == 565
                await login("work_member")
                other = (await client.get(rating)).json()
                assert other["rating"] == 858 and other["history"][0]["rank"] == 1
                assert other["history"][0]["old_rating"] == 433

    asyncio.run(run())
