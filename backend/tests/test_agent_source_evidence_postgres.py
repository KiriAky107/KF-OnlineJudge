"""The API captures owned code evidence for the restricted learning worker."""

import asyncio
import json
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_problems import import_fixture
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.agent_worker import LearningWorker

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_learning_code_requires_opt_in_and_never_returns_raw_source(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        admin = create_async_engine(postgres_url)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"origin": ORIGIN}
                ) as client,
            ):
                password = secrets.token_urlsafe(24)
                own = await app.state.identity.create_account("source_owner", password)
                foreign = await app.state.identity.create_account(
                    "source_other", secrets.token_urlsafe(24)
                )
                login = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "source_owner",
                        "password": password,
                    },
                )
                assert login.status_code == 200
                client.headers["x-csrf-token"] = client.cookies["kf_csrf"]
                problem = await app.state.problems.import_directory(tmp_path / "import")
                import_fixture(tmp_path / "alternate", slug="alternate-review-problem")
                alternate = await app.state.problems.import_directory(tmp_path / "alternate")

                async def submit(actor, code, *, language="c17", version=problem):
                    return await app.state.submissions.create(
                        actor,
                        CreateSubmission(
                            problem_version_id=version,
                            language=language,
                            idempotency_key=uuid4(),
                            source=code,
                        ),
                    )

                before = await submit(own, "// private-code\nint example=1;\n")
                await submit(own, "// synthetic other language", language="cpp20")
                await submit(own, "// synthetic other version", version=alternate)
                current = await submit(own, "// private-code\nint example=2;\n")
                hidden = await submit(foreign, "// foreign-code\nint example=3;\n")
                future = await submit(own, "// future-code\nint example=4;\n")
                fixture = (
                    Path(__file__).parents[2] / "tests/fixtures/contracts/judge-result.valid.json"
                )
                judgement = json.loads(fixture.read_text())
                judgement["binding"]["evaluation_id"] = str(current.evaluation_id)
                judgement["compilation"]["diagnostics"] = (
                    "main.c:2:5: warning: private-diagnostic\nmain.c:999:5: error: out-of-range\n"
                )
                async with admin.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE evaluations SET state='finished', verdict='wrong_answer', "
                            "updated_at=clock_timestamp() WHERE id=:id"
                        ),
                        {"id": before.evaluation_id},
                    )
                    await db.execute(
                        text(
                            "UPDATE submissions SET created_at=clock_timestamp() "
                            "+ interval '2 days' WHERE id=:id"
                        ),
                        {"id": future.submission_id},
                    )
                    await db.execute(
                        text(
                            "UPDATE evaluations SET state='finished', verdict='accepted', "
                            "final_report=CAST(:payload AS jsonb) WHERE id=:id"
                        ),
                        {"payload": json.dumps(judgement), "id": current.evaluation_id},
                    )

                catalog = (await client.get(f"{ROOT}/skills")).json()["versions"]
                worker = LearningWorker(async_sessionmaker(worker_engine, expire_on_commit=False))

                async def setup(purpose, tools, parameters=None):
                    skill = next(
                        v for v in catalog if v["purpose"] == purpose and v["release"] == 2
                    )
                    config = {
                        "enabled": True,
                        "parameters": parameters or {},
                        "authorized_tools": [
                            t for t in skill["available_tools"] if t["name"] in tools
                        ],
                    }
                    response = await client.put(f"{ROOT}/skills/{skill['version_id']}", json=config)
                    assert response.status_code == 200
                    response = await client.post(f"{ROOT}/sessions", json={"purpose": purpose})
                    return skill["version_id"], response.json()["session_id"]

                async def start(skill, session, **inputs):
                    return await client.post(
                        f"{ROOT}/sessions/{session}/runs",
                        json={
                            "skill_version_id": skill,
                            "request_id": str(uuid4()),
                            **inputs,
                        },
                    )

                async def finish(queued):
                    assert queued.status_code == 202
                    assert await worker.run_once()
                    path = f"{ROOT}/runs/{queued.json()['run_id']}"
                    run = await client.get(path)
                    assert run.status_code == 200 and run.json()["state"] == "succeeded"
                    assert "private-code" not in run.text and "private-diagnostic" not in run.text
                    assert "foreign-code" not in run.text
                    return run.json()["result"]

                history = {"submission_history"}
                approved = history | {"source_evidence"}
                review, session = await setup("submission_review", history)
                assert (
                    await start(review, session, include_source_evidence=True)
                ).status_code == 404
                review, session = await setup("submission_review", approved)
                baseline = await finish(await start(review, session))
                assert baseline["code"] is None
                response = await start(
                    review,
                    session,
                    include_source_evidence=True,
                    submission_id=str(hidden.submission_id),
                )
                assert response.status_code == 404
                response = await start(
                    review,
                    session,
                    include_source_evidence=True,
                    submission_id=str(future.submission_id),
                )
                assert response.status_code == 404
                code = (await finish(await start(review, session, include_source_evidence=True)))[
                    "code"
                ]
                assert code["source"]["submission_id"] == str(current.submission_id)
                assert code["comparison"]["earlier"]["submission_id"] == str(before.submission_id)
                assert code["comparison"]["regions"] == [
                    {
                        "kind": "replace",
                        "before_line": 2,
                        "before_count": 1,
                        "after_line": 2,
                        "after_count": 1,
                    }
                ]
                assert code["compiler_locations"] == [{"line": 2, "column": 5, "kind": "warning"}]

                await submit(own, "// synthetic newer submission")
                selected = str(current.submission_id)
                focused = await finish(await start(review, session, submission_id=selected))
                assert focused["anchor_submission_id"] == selected and focused["code"] is None
                assert [row["submission_id"] for row in focused["records"]] == [
                    selected,
                    str(before.submission_id),
                ]
                assert [pair["outcome"] for pair in focused["comparisons"]] == ["later_accepted"]

                queued = await start(review, session, submission_id=str(hidden.submission_id))
                assert queued.status_code == 202 and await worker.run_once()
                denied = (await client.get(f"{ROOT}/runs/{queued.json()['run_id']}")).json()
                assert (
                    denied["state"] == "failed" and denied["failure_code"] == "evidence_unavailable"
                )

                guided, session = await setup("guided_help", approved, {"hint_level": "location"})
                response = await start(
                    guided,
                    session,
                    include_source_evidence=True,
                    submission_id=str(current.submission_id),
                )
                result = await finish(response)
                assert result["delivered_level"] == "location" and "第 2 行" in result["hint"]
                assert result["code"]["comparison"] is None and result["code"]["line_count"] == 2

                broken = await submit(own, "// private-code\nint example=2\n")
                ce = json.loads(fixture.with_name("judge-result.valid-ce.json").read_text())
                ce["binding"]["evaluation_id"] = str(broken.evaluation_id)
                ce["compilation"]["diagnostics"] = (
                    "main.c:2:5: error: expected ';' at end of input\nprivate-diagnostic\n"
                )
                async with admin.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE evaluations SET state='finished', verdict='compilation_error', "
                            "final_report=CAST(:payload AS jsonb) WHERE id=:id"
                        ),
                        {"payload": json.dumps(ce), "id": broken.evaluation_id},
                    )

                for allow_example in (False, True):
                    guided, session = await setup(
                        "guided_help",
                        approved,
                        {"hint_level": "local_change", "allow_code_examples": allow_example},
                    )
                    response = await start(
                        guided,
                        session,
                        include_source_evidence=True,
                        submission_id=str(broken.submission_id),
                    )
                    result = await finish(response)
                    assert result["delivered_level"] == "local_change"
                    assert "第 2 行" in result["hint"] and "补上分号" in result["hint"]
                    assert result["hint_example"] == (
                        "value = next_value;" if allow_example else None
                    )
                    assert result["code"]["compiler_correction"]["kind"] == "missing_semicolon"
                unopted = await finish(
                    await start(guided, session, submission_id=str(broken.submission_id))
                )
                assert unopted["delivered_level"] == "concept" and unopted["hint_example"] is None
                assert unopted["code"] is None
                unclassified = await finish(
                    await start(
                        guided,
                        session,
                        include_source_evidence=True,
                        submission_id=str(current.submission_id),
                    )
                )
                assert unclassified["delivered_level"] == "location"
                assert unclassified["hint_example"] is None
        finally:
            await worker_engine.dispose()
            await admin.dispose()

    asyncio.run(exercise())
