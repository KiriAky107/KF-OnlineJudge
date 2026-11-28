"""Public-sample consent and result delivery through real platform sessions."""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect, migrate
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.contracts.agent_api import ConfirmPublicSampleDiagnostic
from kf_oj.contracts.diagnostic import DiagnosticObservation, UbsanFinding, UbsanObservation
from kf_oj.contracts.judging import Verdict
from kf_oj.modules.problems.learning_models import ProblemLearningMetadata

ORIGIN = "http://localhost:5173"
ROOT = "/api/v1/learning/agent/diagnostics"
MISTAKES = "/api/v1/learning/agent/mistakes"


def test_public_sample_diagnostic_requires_a_strict_confirmation():
    body = dict(submission_id=uuid4(), problem_version_id=uuid4(), sample_ordinal=0)
    assert ConfirmPublicSampleDiagnostic(**body, source_execution_confirmed=True)
    for value in (False, 1, "true", None):
        with pytest.raises(ValueError, match="explicit confirmation"):
            ConfirmPublicSampleDiagnostic(**body, source_execution_confirmed=value)
    with pytest.raises(ValueError):
        ConfirmPublicSampleDiagnostic(**body)


@pytest.mark.parametrize("allow_ubsan", [False, True])
def test_public_sample_diagnostic_routes_require_same_live_owner_and_consent(
    postgres_url, tmp_path, monkeypatch, allow_ubsan
):
    async def exercise():
        monkeypatch.setenv("KF_OJ_LEARNING_DIAGNOSTICS_ENABLED", "true")
        monkeypatch.setenv("KF_OJ_LEARNING_DIAGNOSTICS_UBSAN_ENABLED", str(allow_ubsan))
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)

        def browser():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=ORIGIN,
                headers={"origin": ORIGIN},
            )

        async def login(client, user, password):
            response = await client.post(
                "/api/v1/auth/login", json={"username": user, "password": password}
            )
            assert response.status_code == 200
            client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

        async with (
            app.router.lifespan_context(app),
            browser() as owner,
            browser() as foreign,
            browser() as same_owner_new_session,
        ):
            path = f"{ROOT}/{uuid4()}"
            assert (await owner.get(path)).status_code == 401
            password = secrets.token_urlsafe(24)
            account = await app.state.identity.create_account("diag_http_owner", password)
            other = await app.state.identity.create_account("diag_http_foreign", password)
            await login(owner, "diag_http_owner", password)
            await login(foreign, "diag_http_foreign", password)
            await login(same_owner_new_session, "diag_http_owner", password)
            availability = await owner.get(f"{ROOT}/availability")
            assert availability.json() == (
                {"available": True, "ubsan": True} if allow_ubsan else {"available": True}
            )
            version = await app.state.problems.import_directory(tmp_path / "import")

            async def submit():
                response = await owner.post(
                    "/api/v1/submissions",
                    json={
                        "problem_version_id": str(version),
                        "language": "c17",
                        "source": "int main(void) { return 0; }\n",
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert response.status_code == 201
                return response.json()["submission_id"]

            body = {
                "submission_id": await submit(),
                "problem_version_id": str(version),
                "sample_ordinal": 0,
                "source_execution_confirmed": True,
            }
            db = await connect(postgres_url)
            try:
                context = []
                for account_id in (account.account_id, other.account_id):
                    session, run, skill = uuid4(), uuid4(), uuid4()
                    await db.execute(
                        "INSERT INTO learning_agent_sessions (id,owner_id,purpose) "
                        "VALUES ($1,$2,'submission_review')",
                        session,
                        account_id,
                    )
                    await db.execute(
                        "INSERT INTO learning_agent_runs "
                        "(id,session_id,request_id,skill_version_id,configuration_digest,"
                        "input_digest,state) VALUES ($1,$2,$3,$4,$5,$6,'succeeded')",
                        run,
                        session,
                        uuid4(),
                        skill,
                        "a" * 64,
                        "b" * 64,
                    )
                    await db.execute(
                        "INSERT INTO learning_agent_workflow_data (run_id,input,result) "
                        "VALUES ($1,'{}'::jsonb,$2::jsonb)",
                        run,
                        json.dumps({"anchor_submission_id": body["submission_id"]}),
                    )
                    context.append((run, skill))
                owned_run, owned_skill = context[0]
                assert (
                    await owner.put(path, json=body | {"review_run_id": str(context[1][0])})
                ).status_code == 404
            finally:
                await db.close()
            body["review_run_id"] = str(owned_run)
            assert (
                await owner.put(path, json=body, headers={"x-csrf-token": ""})
            ).status_code == 403
            assert (
                await owner.put(path, json=body | {"source_execution_confirmed": 1})
            ).status_code == 422
            assert (await owner.put(path, json=body | {"sample_ordinal": 1})).status_code == 404
            assert (await foreign.put(path, json=body)).status_code == 404
            assert (await owner.get(path)).status_code == 404

            first = await owner.put(path, json=body)
            assert first.status_code == 202 and first.json()["state"] == "queued"
            assert first.headers["cache-control"] == "no-store"
            assert (await owner.put(path, json=body)).json() == first.json()
            db = await connect(postgres_url)
            try:
                snapshot = await db.fetchval(
                    "SELECT review_context FROM learning_diagnostic_requests WHERE id=$1",
                    UUID(first.json()["request_id"]),
                )
                assert json.loads(snapshot) == {
                    "run_id": str(owned_run),
                    "skill_version_id": str(owned_skill),
                    "configuration_sha256": "a" * 64,
                }
            finally:
                await db.close()
            conflict = await owner.put(path, json=body | {"review_run_id": None})
            assert conflict.status_code == 409
            assert (
                await owner.put(path, json=body | {"submission_id": await submit()})
            ).status_code == 404
            busy = await owner.put(f"{ROOT}/{uuid4()}", json=body)
            assert busy.status_code == 429
            for denied in (foreign, same_owner_new_session):
                assert (await denied.get(path)).status_code == 404
                assert (await denied.post(f"{path}/cancel")).status_code == 404

            queue = app.state.agent_learning.diagnostics
            assert queue is not None
            claim = await queue.claim()
            assert claim is not None and str(claim.request_id) == first.json()["request_id"]
            observed = DiagnosticObservation(o2=Verdict.WRONG_ANSWER, o0=Verdict.ACCEPTED)
            # Supply a fixed trusted observation to exercise delivery, without executing here.
            await queue.acknowledge_stopped(claim, observation=observed)
            result = await owner.get(path)
            assert result.status_code == 200
            assert result.json() == first.json() | {
                "state": "observed",
                "observation": {"o2": "wrong_answer", "o0": "accepted"},
            }
            confirmation = {"confirmed": True, "note": "  手算后确认中间值溢出。  "}
            assert (await owner.put(f"{path}/mistake", json=confirmation)).status_code == 404
            assert (await owner.get(MISTAKES)).json() == {"items": [], "next_before": None}

            ubsan_path = f"{ROOT}/{uuid4()}"
            chosen = await owner.put(ubsan_path, json=body | {"mode": "ubsan"})
            if allow_ubsan:
                assert chosen.status_code == 202
                assert chosen.json()["mode"] == "ubsan" and chosen.json()["sample_ordinal"] == 0
                request_id = chosen.json()["request_id"]
                mistake_path = f"{MISTAKES}/{request_id}"
                confirm_path = f"{ubsan_path}/mistake"
                assert (await owner.put(confirm_path, json=confirmation)).status_code == 404
                assert (await owner.put(ubsan_path, json=body)).status_code == 409
                ubsan_claim = await queue.claim()
                assert ubsan_claim.mode == "ubsan"
                with pytest.raises(ValueError, match="one validated outcome"):
                    await queue.acknowledge_stopped(ubsan_claim, observation=observed)
                ubsan = UbsanObservation(
                    verdict=Verdict.RUNTIME_ERROR,
                    finding=UbsanFinding(category="signed_integer_overflow", line=1, column=4),
                )
                await queue.acknowledge_stopped(ubsan_claim, observation=ubsan)
                recovered = await owner.get(ubsan_path)
                assert recovered.status_code == 200 and recovered.json() == chosen.json() | {
                    "state": "observed",
                    "ubsan": ubsan.model_dump(mode="json"),
                }
                assert (await foreign.get(ubsan_path)).status_code == 404
                assert (await owner.get(mistake_path)).status_code == 404
                assert (await same_owner_new_session.get(MISTAKES)).json()["items"] == []
                agent = "/api/v1/learning/agent"
                skills = (await owner.get(f"{agent}/skills")).json()["versions"]
                skill = next(item for item in skills if item["purpose"] == "focused_training")
                skill_path = f"{agent}/skills/{skill['version_id']}"
                practice_path = f"{MISTAKES}/practice"
                practice = {"skill_version_id": skill["version_id"], "request_id": request_id}
                assert (await owner.post(practice_path, json=practice)).status_code == 404
                for browser in (owner, foreign):
                    configured = await browser.put(
                        skill_path,
                        json={
                            "enabled": True,
                            "parameters": {
                                "minimum_difficulty": "easy",
                                "maximum_difficulty": "easy",
                            },
                            "authorized_tools": skill["available_tools"],
                        },
                    )
                    assert configured.status_code == 200, configured.text
                assert (await owner.post(practice_path, json=practice)).status_code == 404
                for denied in (foreign, same_owner_new_session):
                    assert (await denied.put(confirm_path, json=confirmation)).status_code == 404
                assert (
                    await owner.put(confirm_path, json=confirmation, headers={"x-csrf-token": ""})
                ).status_code == 403
                assert (
                    await owner.put(confirm_path, json=confirmation | {"confirmed": 1})
                ).status_code == 422
                assert (
                    await owner.put(
                        confirm_path, json=confirmation | {"submission_id": str(uuid4())}
                    )
                ).status_code == 422
                one, two = await asyncio.gather(
                    owner.put(confirm_path, json=confirmation),
                    owner.put(confirm_path, json=confirmation),
                )
                assert one.status_code == two.status_code == 200
                record = one.json()
                assert record == two.json()
                assert one.headers["cache-control"] == "no-store"
                assert record == {
                    "request_id": request_id,
                    "submission_id": body["submission_id"],
                    "problem_version_id": body["problem_version_id"],
                    "sample_ordinal": body["sample_ordinal"],
                    "finding": ubsan.finding.model_dump(mode="json"),
                    "note": confirmation["note"].strip(),
                    "created_at": record["created_at"],
                    "updated_at": record["created_at"],
                }
                assert (await foreign.get(MISTAKES)).json()["items"] == []
                assert (await foreign.get(mistake_path)).status_code == 404
                assert (
                    await foreign.patch(mistake_path, json={"note": "foreign"})
                ).status_code == 404
                assert (await foreign.delete(mistake_path)).status_code == 204
                assert (await same_owner_new_session.get(mistake_path)).json() == record
                assert (
                    await same_owner_new_session.patch(mistake_path, json={"note": "a" * 601})
                ).status_code == 422
                edited = await same_owner_new_session.patch(
                    mistake_path, json={"note": "下次先检查乘法使用的类型。"}
                )
                assert edited.status_code == 200
                updated = edited.json()
                assert updated == record | {
                    "note": "下次先检查乘法使用的类型。",
                    "updated_at": updated["updated_at"],
                }
                assert updated["updated_at"] != record["updated_at"]
                assert (await owner.put(confirm_path, json=confirmation)).json() == updated
                assert (await same_owner_new_session.get(MISTAKES)).json() == {
                    "items": [updated],
                    "next_before": None,
                }

                async def tagged_problem(slug, tags, tier):
                    fixture = tmp_path / slug
                    import_fixture(fixture, slug=slug)
                    item = await app.state.problems.import_directory(fixture)
                    async with app.state.sessions.begin() as db:
                        db.add(
                            ProblemLearningMetadata(
                                problem_version_id=item, tags=tags, difficulty=tier, revision=1
                            )
                        )
                    return item

                untagged = await owner.post(practice_path, json=practice)
                assert untagged.status_code == 200
                assert not untagged.json()["items"]
                assert "原题目版本尚未标注知识点。" in untagged.json()["gaps"]
                async with app.state.sessions.begin() as db:
                    db.add(
                        ProblemLearningMetadata(
                            problem_version_id=version,
                            tags=[*(f"标签{i}" for i in range(11)), "整数范围"],
                            difficulty="easy",
                            revision=1,
                        )
                    )
                similar = await tagged_problem("practice-similar", ["整数范围"], "easy")
                await tagged_problem("practice-harder", ["整数范围"], "hard")
                await tagged_problem("practice-unrelated", ["图论"], "easy")
                assert (await foreign.post(practice_path, json=practice)).status_code == 404
                assert (
                    await owner.post(practice_path, json=practice | {"submission_id": str(uuid4())})
                ).status_code == 404
                assert (
                    await owner.post(practice_path, json=practice | {"available_minutes": 181})
                ).status_code == 422
                assert (
                    await owner.post(practice_path, json=practice, headers={"x-csrf-token": ""})
                ).status_code == 403
                lookup = await same_owner_new_session.post(
                    practice_path,
                    json={
                        "skill_version_id": skill["version_id"],
                        "submission_id": body["submission_id"],
                    },
                )
                assert lookup.status_code == 200, lookup.text
                assert lookup.json()["items"] == [
                    {
                        "problem_version_id": str(similar),
                        "title": "Synthetic problem",
                        "shared_tags": ["整数范围"],
                        "estimated_minutes": None,
                    }
                ]
                timed = await owner.post(practice_path, json=practice | {"available_minutes": 30})
                assert timed.status_code == 200, timed.text
                preview = timed.json()
                assert preview["available_minutes"] == 30
                assert preview["evidence"] == {
                    key: value
                    for key, value in record.items()
                    if key not in {"note", "created_at", "updated_at"}
                }
                assert (preview["review_minutes"], preview["verification_minutes"]) == (5, 5)
                assert preview["items"] == [lookup.json()["items"][0] | {"estimated_minutes": 20}]
                assert (await owner.get("/api/v1/trainings")).json()["items"] == []
                with pytest.raises(AssertionError, match="personal mistake records exist"):
                    await asyncio.to_thread(
                        migrate, postgres_url, "downgrade", "0054_learning_diagnostic_ubsan"
                    )
            else:
                assert chosen.status_code == 404

            pending = f"{ROOT}/{uuid4()}"
            assert (await owner.put(pending, json=body)).status_code == 202
            assert (await owner.post(f"{pending}/cancel")).status_code == 204
            assert (await owner.get(pending)).json()["state"] == "cancelled"

            operator = await connect(postgres_url)
            try:
                await operator.execute(
                    "INSERT INTO scheduled_contests VALUES "
                    "('ar08-http','AR-08 HTTP',"
                    "now()-interval '1 minute',now()+interval '1 hour')"
                )
                await operator.execute(
                    "INSERT INTO contest_entrants VALUES ($1,'ar08-http')",
                    account.account_id,
                )
                assert (await owner.get(path)).status_code == 403
                assert (await owner.put(f"{ROOT}/{uuid4()}", json=body)).status_code == 403
                assert (await owner.get(MISTAKES)).status_code == 403
                if allow_ubsan:
                    assert (await owner.delete(mistake_path)).status_code == 403
                    assert (await owner.post(practice_path, json=practice)).status_code == 403
                await operator.execute("DELETE FROM contest_entrants WHERE contest_key='ar08-http'")
                assert (await owner.get(path)).status_code == 200
            finally:
                await operator.close()
            logout = await owner.post("/api/v1/auth/logout")
            assert logout.status_code == 204
            assert (await owner.get(path)).status_code == 401
            assert (await same_owner_new_session.get(path)).status_code == 404

            if allow_ubsan:
                assert (await same_owner_new_session.get(mistake_path)).json() == updated
                app.state.agent_learning.diagnostics = None
                assert (await same_owner_new_session.delete(mistake_path)).status_code == 204
                assert (await same_owner_new_session.delete(mistake_path)).status_code == 204
                assert (await same_owner_new_session.get(mistake_path)).status_code == 404
                assert (await same_owner_new_session.get(MISTAKES)).json()["items"] == []
                assert (
                    await same_owner_new_session.post(practice_path, json=practice)
                ).status_code == 404
                with pytest.raises(
                    AssertionError, match="cannot downgrade while UBSan diagnostics exist"
                ):
                    await asyncio.to_thread(
                        migrate, postgres_url, "downgrade", "0053_learning_review_mcp"
                    )
            else:
                await asyncio.to_thread(
                    migrate, postgres_url, "downgrade", "0053_learning_review_mcp"
                )
                await asyncio.to_thread(migrate, postgres_url, "upgrade", "head")
                operator = await connect(postgres_url)
                try:
                    old = await operator.fetchrow(
                        "SELECT mode, o2, o0, ubsan FROM learning_diagnostic_requests WHERE id=$1",
                        first.json()["request_id"],
                    )
                    assert tuple(old) == ("optimization", "wrong_answer", "accepted", None)
                finally:
                    await operator.close()

    asyncio.run(exercise())
