"""Real PostgreSQL owner projection/pagination acceptance; no program execution."""

import asyncio
import json
import secrets
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

from kf_oj.contracts.self_tests import CreateSelfTest
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings
from kf_oj.modules.submissions.queries import cursor_decode


def test_cursor_and_control_configuration_fail_closed():
    for value in ["garbage", "_" * 129, "", "eHx4"]:
        with pytest.raises(ValueError):
            cursor_decode(value)
    for url in ["http://localhost:8100", "https://127.0.0.1:8100", "http://127.0.0.1:8100/path"]:
        with pytest.raises(ValueError):
            Settings(_env_file=None, coordinator_url=url, coordinator_token="a" * 64)
    Settings(_env_file=None, coordinator_url="http://127.0.0.1:8100", coordinator_token="a" * 64)


def test_catalog_counts_and_editor_preserve_published_history(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            password = secrets.token_urlsafe(24)
            owner = await app.state.identity.create_account("catalog_reader", password)
            version = await app.state.problems.import_directory(tmp_path / "import")
            evaluations = [
                (
                    await app.state.submissions.create(
                        owner,
                        CreateSubmission(
                            problem_version_id=version,
                            language="c17",
                            idempotency_key=uuid4(),
                            source=f"synthetic catalog fixture {i}",
                        ),
                    )
                ).evaluation_id
                for i in range(4)
            ]
            await app.state.self_tests.create(
                owner,
                CreateSelfTest(
                    problem_version_id=version,
                    language="c17",
                    idempotency_key=uuid4(),
                    source="fixture",
                ),
            )
            admin = create_async_engine(postgres_url)
            try:
                async with admin.begin() as conn:
                    await conn.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                    for evaluation, verdict in zip(
                        evaluations[:3], ("accepted", "wrong_answer", "system_error"), strict=True
                    ):
                        await conn.execute(
                            text(
                                "UPDATE evaluations SET state='finished', verdict=:verdict, "
                                "final_report_digest=:digest WHERE id=:id"
                            ),
                            {"id": evaluation, "verdict": verdict, "digest": "a" * 64},
                        )
            finally:
                await admin.dispose()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client:
                (item,) = (await client.get("/api/v1/problems")).raise_for_status().json()["items"]
                editor_path = f"/api/v1/admin/problems/{item['id']}/editor"
                revisions_path = f"/api/v1/admin/problems/{item['id']}/revisions"
                origin = {"Origin": "http://localhost:8000"}
                credentials = {"username": owner.username, "password": password}
                (
                    await client.post("/api/v1/auth/login", json=credentials, headers=origin)
                ).raise_for_status()
                assert (await client.get(editor_path)).status_code == 403
                connection = await connect(postgres_url)
                try:
                    await set_administrator(connection, owner.account_id, True)
                finally:
                    await connection.close()
                login = (
                    (await client.post("/api/v1/auth/login", json=credentials, headers=origin))
                    .raise_for_status()
                    .json()
                )
                headers = origin | {"X-CSRF-Token": login["csrf_token"]}
                original = (await client.get(editor_path)).raise_for_status().json()
                assert original["detail"]["version_id"] == str(version)
                sample = {"encoding": "base64", "input": "MQo=", "output": "Mgo="}
                body = {
                    "operation_id": str(uuid4()),
                    "expected_active_version_id": str(version),
                    "expected_metadata_revision": original["metadata"]["revision"],
                    "title": "修订后的题目",
                    "statement": "新题面，包含公式 $x+1$。",
                    "samples": [sample],
                    "metadata": {"tags": ["枚举"], "difficulty": "medium"},
                }
                receipt = (
                    (await client.post(revisions_path, headers=headers, json=body))
                    .raise_for_status()
                    .json()
                )
                assert receipt["problem"]["revision"] == original["detail"]["revision"] + 1
                same = (
                    (await client.post(revisions_path, headers=headers, json=body))
                    .raise_for_status()
                    .json()
                )
                assert same == receipt
                stale = await client.post(
                    revisions_path, headers=headers, json=body | {"operation_id": str(uuid4())}
                )
                assert stale.status_code == 409
                revised = (await client.get(editor_path)).raise_for_status().json()
                assert revised["detail"]["samples"] == [sample]
                assert revised["metadata"]["tags"] == ["枚举"]
                assert revised["detail"]["run_limits"] == original["detail"]["run_limits"]
                assert revised["case_count"] == original["case_count"]
                old = (
                    (await client.get(f"/api/v1/problems/versions/{version}"))
                    .raise_for_status()
                    .json()
                )
                assert old["statement"] == original["detail"]["statement"]
                (current,) = (
                    (await client.get("/api/v1/problems")).raise_for_status().json()["items"]
                )
                assert current["title"] == body["title"]
                assert current["version_id"] == receipt["published_version_id"]
                assert current["total_submissions"] == 4
                assert (current["accepted_submissions"], current["judged_submissions"]) == (1, 2)
                assert (
                    current["verdict_counts"]["accepted"]
                    == current["verdict_counts"]["wrong_answer"]
                    == 1
                )
                assert "system_error" not in current["verdict_counts"]

    asyncio.run(run())


@pytest.mark.parametrize("wire_version", [1, 2])
def test_actual_owned_history_source_result_and_cancel(
    postgres_url, tmp_path, monkeypatch, wire_version
):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        async with app.router.lifespan_context(app):
            passwords = [secrets.token_urlsafe(24), secrets.token_urlsafe(24)]
            owners = [
                await app.state.identity.create_account(name, password)
                for name, password in zip(["reader_one", "reader_two"], passwords, strict=True)
            ]
            version = await app.state.problems.import_directory(tmp_path / "import")
            records = []
            for i in range(4):
                records.append(
                    await app.state.submissions.create(
                        owners[0],
                        CreateSubmission(
                            problem_version_id=version,
                            language="c17",
                            idempotency_key=uuid4(),
                            source="synthetic private source " + str(i),
                        ),
                    )
                )
            # Synthetic contract projection only, never claimed as a real judgement.
            fixture = Path(__file__).parents[2] / (
                "tests/fixtures/contracts/judge-result.valid.json"
                if wire_version == 1
                else "tests/fixtures/contracts-v2/judge-result.valid-c17.json"
            )
            report = json.loads(fixture.read_text())
            report["binding"]["evaluation_id"] = str(records[0].evaluation_id)
            if wire_version == 2:
                report["compilation"]["program"]["binding"]["evaluation_id"] = str(
                    records[0].evaluation_id
                )
            report_json = json.dumps(report)
            admin = create_async_engine(postgres_url)
            async with admin.begin() as conn:
                await conn.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                await conn.execute(
                    text(
                        "UPDATE evaluations SET state='finished',verdict='accepted',"
                        "final_report=CAST(:report AS jsonb),final_report_digest=:digest "
                        "WHERE id=:id"
                    ),
                    {
                        "id": records[0].evaluation_id,
                        "report": report_json,
                        "digest": sha256(report_json.encode()).hexdigest(),
                    },
                )
            await admin.dispose()

            class CancelProbe:
                accepted = True
                seen = []

                async def cancel(self, evaluation_id):
                    self.seen.append(evaluation_id)
                    return self.accepted

            probe = CancelProbe()
            app.state.submission_queries.cancellation = probe
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
            ) as client:
                status_path = f"/api/v1/submissions/problem-status/{version}"
                assert (await client.get(status_path)).status_code == 401
                first = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "reader_one", "password": passwords[0]},
                    headers={"Origin": "http://localhost:8000"},
                )
                assert first.status_code == 200
                status = await client.get(status_path)
                assert status.status_code == 200 and status.headers["Cache-Control"] == "no-store"
                assert status.json()["solved"] is True
                assert status.json()["latest"]["submission_id"] == str(records[-1].submission_id)
                assert status.json()["latest"]["state"] == "queued"
                assert "result" not in status.json()["latest"]
                csrf = {
                    "Origin": "http://localhost:8000",
                    "X-CSRF-Token": first.json()["csrf_token"],
                }
                page = await client.get("/api/v1/submissions?limit=2")
                assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
                after = page.json()["next_after"]
                # Concurrent insertion must not shift the cursor into repeats or omit old rows.
                await app.state.submissions.create(
                    owners[0],
                    CreateSubmission(
                        problem_version_id=version,
                        language="cpp20",
                        idempotency_key=uuid4(),
                        source="new synthetic private source",
                    ),
                )
                tail = await client.get("/api/v1/submissions", params={"limit": 2, "after": after})
                seen = [i["submission_id"] for i in page.json()["items"] + tail.json()["items"]]
                assert len(set(seen)) == 4 and set(seen) == {str(r.submission_id) for r in records}
                assert tail.json()["next_after"] is None
                base = f"/api/v1/submissions/{records[0].submission_id}"
                public = await client.get(base)
                assert public.status_code == 200 and public.json()["verdict"] == "accepted"
                projection = public.json()["result"]
                assert projection["cpu_time_us"] == report["run_cpu_time_us"]
                assert projection["memory_peak_bytes"] == report["run_memory_peak_bytes"]
                assert all(
                    key not in public.text
                    for key in ["case_id", "digest", "lease", "expected", "manifest", "program"]
                )
                source = await client.get(base + "/source")
                assert source.json()["source"] == "synthetic private source 0"
                waiting = await client.get(f"/api/v1/submissions/{records[1].submission_id}")
                assert waiting.json()["result"] is None and waiting.json()["verdict"] is None
                cancel = f"/api/v1/submissions/{records[1].submission_id}/cancel"
                assert (await client.post(cancel)).status_code == 403
                assert (await client.post(cancel, headers=csrf)).status_code == 202
                assert probe.seen == [records[1].evaluation_id]
                assert (await client.get(cancel.removesuffix("/cancel"))).json()[
                    "state"
                ] == "queued"
                probe.accepted = False
                assert (await client.post(cancel, headers=csrf)).status_code == 503
                assert (await client.get("/api/v1/submissions?after=invalid")).status_code == 422
                second = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "reader_two", "password": passwords[1]},
                    headers={"Origin": "http://localhost:8000"},
                )
                csrf["X-CSRF-Token"] = second.json()["csrf_token"]
                assert (await client.get("/api/v1/submissions")).json()["items"] == []
                assert (await client.get(status_path)).json() == {"solved": False, "latest": None}
                for suffix in ["", "/source"]:
                    response = await client.get(base + suffix)
                    assert (
                        response.status_code == 404
                        and response.headers["Cache-Control"] == "no-store"
                    )
                assert (await client.post(cancel, headers=csrf)).status_code == 404
                assert len(probe.seen) == 2
                assert (await client.post("/api/v1/auth/logout", headers=csrf)).status_code == 204
                assert (await client.get(base)).status_code == 401

    asyncio.run(run())
