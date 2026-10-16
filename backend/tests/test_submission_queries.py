"""Real PostgreSQL owner projection/pagination acceptance; no program execution."""

import asyncio
import json
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_submission_create import setup_app

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
            admin = create_async_engine(postgres_url)
            async with admin.begin() as conn:
                await conn.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                await conn.execute(
                    text(
                        "UPDATE evaluations SET state='finished',verdict='accepted',"
                        "final_report=CAST(:report AS jsonb) WHERE id=:id"
                    ),
                    {"id": records[0].evaluation_id, "report": json.dumps(report)},
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
                first = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "reader_one", "password": passwords[0]},
                    headers={"Origin": "http://localhost:8000"},
                )
                assert first.status_code == 200
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
