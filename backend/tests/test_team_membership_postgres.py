"""Exercise real team authority through HTTP sessions and PostgreSQL."""

import asyncio
import secrets
from uuid import UUID, uuid4

import httpx
from test_submission_create import setup_app

from kf_oj.modules.teams.public import TeamPermissionDenied, require_team_role

_ORIGIN = "http://localhost:5173"
_TEAMS = "/api/v1/teams"


def test_team_creation_application_and_revocation(postgres_url, tmp_path, monkeypatch):
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
            await app.state.identity.create_account(username, password)
            response = await browser.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert response.status_code == 200
            browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
            return response.json()["user"]["id"]

        async with (
            app.router.lifespan_context(app),
            client() as captain,
            client() as applicant,
            client() as outsider,
            client() as guest,
        ):
            captain_id = await authenticate(captain, "captain_test")
            applicant_id = await authenticate(applicant, "applicant_test")
            await authenticate(outsider, "other_captain_test")
            body = {
                "idempotency_key": str(uuid4()),
                "name": " 算法小队 ",
                "description": "准备队内练习",
            }
            assert (await guest.get(_TEAMS)).json()["items"] == []
            assert (await guest.post(_TEAMS, json=body)).status_code == 401
            assert (
                await captain.post(_TEAMS, json=body, headers={"x-csrf-token": ""})
            ).status_code == 403

            created, retry = await asyncio.gather(
                captain.post(_TEAMS, json=body), captain.post(_TEAMS, json=body)
            )
            assert created.status_code == retry.status_code == 201, created.text
            team = created.json()
            assert retry.json() == team
            assert team["name"] == "算法小队" and team["member_count"] == 1
            assert (
                await captain.post(_TEAMS, json={**body, "name": "其他名字"})
            ).status_code == 409

            other = await outsider.post(_TEAMS, json=body)
            assert other.status_code == 201 and other.json()["id"] != team["id"]
            assert (await guest.get(_TEAMS, params={"search": "算法"})).status_code == 200
            assert len((await guest.get(_TEAMS, params={"search": "算法"})).json()["items"]) == 2
            assert (await guest.get(_TEAMS, params={"search": "%"})).json()["items"] == []

            profile = f"{_TEAMS}/{team['id']}"
            mine = f"{profile}/membership"
            roster = f"{profile}/members"
            decision = f"{roster}/{applicant_id}"
            assert (await guest.get(profile)).json() == team
            assert (await guest.get(mine)).status_code == 401
            assert (await guest.get(roster)).status_code == 401
            assert (await captain.get(mine)).json() == {"role": "captain"}
            assert (await applicant.get(mine)).json() == {"role": None}
            assert (await applicant.put(mine)).json() == {"role": "pending"}
            assert (await applicant.put(mine)).json() == {"role": "pending"}
            assert (await guest.get(profile)).json()["member_count"] == 1
            assert (await applicant.get(roster)).status_code == 403
            assert (await applicant.put(decision, json={"decision": "approve"})).status_code == 403
            assert (await outsider.put(decision, json={"decision": "approve"})).status_code == 403
            assert (await captain.delete(mine)).status_code == 409
            assert (
                await captain.put(f"{roster}/{captain_id}", json={"decision": "remove"})
            ).status_code == 409
            assert (await captain.get(roster, params={"offset": -1})).status_code == 422
            assert (await captain.get(roster, params={"offset": 50})).json() == {
                "items": [],
                "next_offset": None,
            }
            assert (await captain.get(roster)).json()["items"] == [
                {"account_id": applicant_id, "username": "applicant_test", "role": "pending"}
            ]

            assert (await applicant.delete(mine)).status_code == 204
            assert (await captain.put(decision, json={"decision": "approve"})).status_code == 404
            assert (await applicant.put(mine)).json() == {"role": "pending"}
            assert (await captain.put(decision, json={"decision": "approve"})).json() == {
                "role": "member",
            }
            assert (await applicant.get(mine)).json() == {"role": "member"}
            assert (await applicant.put(mine)).json() == {"role": "member"}
            assert (await guest.get(profile)).json()["member_count"] == 2
            assert (await captain.get(roster, params={"role": "member"})).json()["items"][0][
                "account_id"
            ] == applicant_id

            identity = app.state.identity
            principal = await identity.authenticate(applicant.cookies["kf_session"])
            team_id = UUID(team["id"])
            async with app.state.teams.sessions.begin() as session:
                assert await require_team_role(session, identity, principal, team_id) == "member"
            assert (await captain.put(decision, json={"decision": "remove"})).json() == {
                "role": None,
            }
            assert (await guest.get(profile)).json()["member_count"] == 1
            async with app.state.teams.sessions.begin() as session:
                try:
                    await require_team_role(session, identity, principal, team_id)
                except TeamPermissionDenied:
                    pass
                else:
                    raise AssertionError("removed member retained team access")
            assert (await applicant.get(mine)).json() == {"role": None}
            assert (await applicant.put(mine)).json() == {"role": "pending"}
            assert (await captain.put(decision, json={"decision": "approve"})).status_code == 200
            assert (await applicant.delete(mine)).status_code == 204
            assert (await applicant.get(mine)).json() == {"role": None}

    asyncio.run(exercise())
