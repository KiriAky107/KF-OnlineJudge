"""Source-specific counts from durable training drafts and graded exercises."""

import asyncio
import json
import secrets
from datetime import timedelta
from uuid import uuid4

import httpx
from conftest import connect
from test_authority_operations import authority
from test_submission_create import setup_app

from kf_oj.contracts.learning import CreateTraining
from kf_oj.contracts.submissions import CreateSubmission

_ORIGIN = "http://localhost:5173"
_TRAINING = "/api/v1/admin/learning/practice"


async def seed_training_observations(app, url, admin, version, password):
    db = await connect(url)
    try:
        now = await db.fetchval("SELECT clock_timestamp()")
        began = now - timedelta(days=12)

        async def actor(label):
            username = f"lab_{label}"
            await app.state.identity.create_account(username, password)
            issued = await app.state.identity.login(username, password, peer="127.0.0.1")
            return issued.principal

        async def plan(account):
            return await app.state.learning.create(
                account, CreateTraining(idempotency_key=uuid4(), title="队内练习", count=1)
            )

        async def dated_plan(account, *, age=12):
            created = await plan(account)
            await db.execute(
                "UPDATE learning_plans SET created_at=$2 WHERE id=$1",
                created.id,
                now - timedelta(days=age),
            )
            return created

        async def finish(account, verdict, *, offset):
            submitted = await app.state.submissions.create(
                account,
                CreateSubmission(
                    idempotency_key=uuid4(),
                    problem_version_id=version,
                    language="c17",
                    source="int main(void) { return 0; }",
                ),
            )
            at = began + timedelta(days=offset)
            await db.execute(
                "UPDATE submissions SET created_at=$2 WHERE id=$1", submitted.submission_id, at
            )
            await db.execute(
                "UPDATE evaluations SET state='finished',verdict=$2,updated_at=$3 WHERE id=$1",
                submitted.evaluation_id,
                verdict,
                at + timedelta(seconds=1),
            )

        async def draft(account, *, purpose="focused_training", saved=None, search=False, items=1):
            session_id, run_id = uuid4(), uuid4()
            await db.execute(
                "INSERT INTO learning_agent_sessions(id,owner_id,purpose) VALUES ($1,$2,$3)",
                session_id,
                account.account_id,
                purpose,
            )
            await db.execute(
                "INSERT INTO learning_agent_runs "
                "(id,session_id,request_id,skill_version_id,configuration_digest,"
                "input_digest,state) "
                "VALUES ($1,$2,$3,$4,$5,$6,'succeeded')",
                run_id,
                session_id,
                uuid4(),
                uuid4(),
                "2" * 64,
                "3" * 64,
            )
            await db.execute(
                "INSERT INTO learning_agent_workflow_data(run_id,input,result) "
                "VALUES ($1,'{}'::jsonb,$2::jsonb)",
                run_id,
                json.dumps(
                    {
                        "purpose": purpose,
                        "items": [{"problem_version_id": str(version)}] * items,
                        "training_set": {"search": search},
                    }
                ),
            )
            await db.execute(
                "INSERT INTO learning_agent_tasks(run_id,authorization_version,session_proof) "
                "VALUES ($1,1,$2)",
                run_id,
                "4" * 64,
            )
            finished = began - timedelta(hours=1)
            await db.execute(
                "INSERT INTO learning_agent_timings(run_id,first_claimed_at,finished_at) "
                "VALUES ($1,$2,$3)",
                run_id,
                finished - timedelta(seconds=1),
                finished,
            )
            if saved is not None:
                await db.execute(
                    "INSERT INTO learning_agent_plan_links(run_id,plan_id,created_at) "
                    "VALUES ($1,$2,$3)",
                    run_id,
                    saved.id,
                    began,
                )

        direct = await actor("direct")
        await dated_plan(direct)
        await dated_plan(direct, age=11)
        await finish(direct, "accepted", offset=3)
        solved = await actor("solved")
        await dated_plan(solved)
        await finish(solved, "accepted", offset=-1)
        immature = await actor("immature")
        await dated_plan(immature, age=3)

        assisted = await actor("assisted")
        ready = await dated_plan(assisted)
        await draft(assisted, saved=ready)
        await draft(assisted)
        await draft(assisted, search=True)
        await draft(assisted, items=0)
        await draft(assisted, purpose="periodic_review")
        await finish(assisted, "wrong_answer", offset=2)
        await finish(assisted, "accepted", offset=8)
        idle = await actor("idle")
        idle_plan = await dated_plan(idle)
        await draft(idle, saved=idle_plan, purpose="periodic_review")

        shared = await actor("shared")
        origin = await plan(admin)
        publication = await app.state.learning.publish_official(
            admin, origin.id, app.state.identity
        )
        copy = await app.state.learning.start_official(shared, publication.id, app.state.identity)
        await db.execute("UPDATE learning_plans SET created_at=$2 WHERE id=$1", copy.id, began)
        await finish(shared, "accepted", offset=1)
    finally:
        await db.close()


def test_training_observation_windows_and_separate_sources(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        password = secrets.token_urlsafe(24)

        def browser():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=_ORIGIN,
                headers={"origin": _ORIGIN},
            )

        async with (
            app.router.lifespan_context(app),
            browser() as admin,
            browser() as member,
        ):
            await app.state.identity.create_account("observation_admin", password)
            await app.state.identity.create_account("observation_member", password)
            db = await connect(postgres_url)
            try:
                assert (await authority(db, "observation_admin", "bootstrap"))["changed"]
            finally:
                await db.close()
            for client, username in ((admin, "observation_admin"), (member, "observation_member")):
                logged = await client.post(
                    "/api/v1/auth/login", json={"username": username, "password": password}
                )
                assert logged.status_code == 200, logged.text
            assert (await member.get(_TRAINING)).status_code == 403
            principal = await app.state.identity.authenticate(admin.cookies["kf_session"])
            version = await app.state.problems.import_directory(tmp_path / "import")
            await seed_training_observations(app, postgres_url, principal, version, password)

            report = await admin.get(_TRAINING, params={"days": 30})
            assert report.status_code == 200, report.text
            assert report.headers["cache-control"] == "no-store"
            payload = report.json()
            assert payload["follow_up_days"] == 7
            assert payload["drafts"] == [
                {"purpose": "focused_training", "draft_count": 2, "saved_count": 1},
                {"purpose": "periodic_review", "draft_count": 2, "saved_count": 1},
            ]
            assert payload["practice"] == [
                {
                    "cohort": "direct",
                    "exercise_count": 1,
                    "attempted_count": 1,
                    "accepted_count": 1,
                },
                {
                    "cohort": "assistant_saved",
                    "exercise_count": 2,
                    "attempted_count": 1,
                    "accepted_count": 0,
                },
                {
                    "cohort": "shared",
                    "exercise_count": 1,
                    "attempted_count": 1,
                    "accepted_count": 1,
                },
            ]

            recent = (await admin.get(_TRAINING, params={"days": 7})).json()
            assert all(draft["draft_count"] == 0 for draft in recent["drafts"])
            assert recent["practice"] == payload["practice"]
            assert (await admin.get(_TRAINING, params={"days": 0})).status_code == 422

    asyncio.run(exercise())
