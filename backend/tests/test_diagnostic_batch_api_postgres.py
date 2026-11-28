"""Exercise registered choices and persistent batch receipts through platform sessions."""

import asyncio
import json
import secrets
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from conftest import connect
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_diagnostic_batch_queue_postgres import registered_boundary
from test_problems import import_fixture, platform_role

from kf_oj.api.app import create_app
from kf_oj.contracts.diagnostic_batch import BatchDiagnosticSummary, RegisteredBatchOffer
from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.contracts.learning_model import ModelHint
from kf_oj.core.settings import Settings
from kf_oj.infra.review_mcp import review_session
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.learning.diagnostic_queue import DiagnosticQueue
from kf_oj.workers.diagnostic_batch import preflight_registered_batch

_ORIGIN = "http://localhost:5173"
_AGENT = "/api/v1/learning/agent"
_ROOT = f"{_AGENT}/diagnostic-batches"


def test_registered_batch_options_accept_only_the_original_confirming_session(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        _, policy = import_fixture(tmp_path / "import")
        engine = create_async_engine(postgres_url)
        platform_role(engine)
        monkeypatch.setattr("kf_oj.api.app.build_engine", lambda _: engine)
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url=postgres_url,
            artifact_root=tmp_path / "artifacts",
            judge_admission=policy,
            learning_diagnostics_enabled=True,
        )
        provision = create_app(settings)
        async with provision.router.lifespan_context(provision):
            version = await provision.state.problems.import_directory(tmp_path / "import")
        registry, digest = registered_boundary(tmp_path, version)
        offer = RegisteredBatchOffer(
            manifest_sha256=digest,
            title="边界资源测量",
            purpose="boundary",
            problem_version_id=version,
            language="c17",
        )
        app = create_app(settings.model_copy(update={"learning_diagnostic_batch_offers": (offer,)}))

        def browser():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=_ORIGIN, headers={"origin": _ORIGIN}
            )

        async def login(client, name, password):
            res = await client.post(
                "/api/v1/auth/login", json={"username": name, "password": password}
            )
            assert res.status_code == 200
            client.headers["x-csrf-token"] = client.cookies["kf_csrf"]

        async with (
            app.router.lifespan_context(app),
            browser() as owner,
            browser() as foreign,
            browser() as new_session,
        ):
            path = f"{_ROOT}/{uuid4()}"
            assert (await owner.get(path)).status_code == 401
            password = secrets.token_urlsafe(24)
            for name in ("batch_api_owner", "batch_api_foreign"):
                await app.state.identity.create_account(name, password)
            await login(owner, "batch_api_owner", password)
            await login(foreign, "batch_api_foreign", password)
            await login(new_session, "batch_api_owner", password)
            status = await owner.get("/api/v1/learning/agent/diagnostics/availability")
            assert status.json() == {"available": True, "registered_batches": True}
            submitted = await owner.post(
                "/api/v1/submissions",
                json={
                    "problem_version_id": str(version),
                    "language": "c17",
                    "source": "int main(void) { return 0; }",
                    "idempotency_key": str(uuid4()),
                },
            )
            assert submitted.status_code == 201
            params = {
                "submission_id": submitted.json()["submission_id"],
                "problem_version_id": str(version),
            }
            choices = await owner.get(f"{_ROOT}/options", params=params)
            assert choices.json() == {
                "options": [
                    {"manifest_sha256": digest, "title": "边界资源测量", "purpose": "boundary"}
                ]
            }
            assert (await foreign.get(f"{_ROOT}/options", params=params)).status_code == 404
            provider_views = []

            class BatchReviewModel:
                supports_review_mcp = True
                accepts_source_excerpt = False

                async def hint(self, content):
                    raise AssertionError("batch review must use its MCP session")

                async def review_with_tools(self, content, resources, continue_request):
                    async with review_session(resources) as mcp:
                        available = (await mcp.list_tools()).tools
                        assert any(tool.name == "batch_diagnostics" for tool in available)
                        resolved = await mcp.call_tool("batch_diagnostics", {})
                        assert not resolved.isError
                        provider_views.append(json.loads(resolved.content[0].text))
                    requested = ("batch_diagnostics",)
                    assert await continue_request(requested)
                    return ModelHint(
                        text="我会根据已登记的诊断证据继续复盘。",
                        model="batch-review-fixture",
                        response_id="batch-review-fixture",
                        latency_ms=1,
                        review_resources=requested,
                    )

            operator = await connect(postgres_url)
            try:
                async with operator.transaction():
                    await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                    await operator.execute(
                        "UPDATE evaluations SET state='finished',verdict='wrong_answer' "
                        "WHERE id=$1",
                        UUID(submitted.json()["evaluation_id"]),
                    )
            finally:
                await operator.close()

            worker_engine = create_async_engine(postgres_url)

            @event.listens_for(worker_engine.sync_engine, "connect")
            def worker_role(connection, _):
                cursor = connection.cursor()
                cursor.execute("SET ROLE kf_oj_learning_worker")
                cursor.close()

            learning = LearningWorker(
                async_sessionmaker(worker_engine, expire_on_commit=False),
                BatchReviewModel(),
                review_batch_offers=(offer,),
            )
            catalog = (await owner.get(f"{_AGENT}/skills")).json()["versions"]
            skill = next(item for item in catalog if item["purpose"] == "submission_review")
            skill_id = skill["version_id"]
            assert (
                await owner.put(
                    f"{_AGENT}/skills/{skill_id}",
                    json={"enabled": True, "authorized_tools": skill["available_tools"]},
                )
            ).status_code == 200

            async def conversation():
                created = await owner.post(
                    f"{_AGENT}/sessions", json={"purpose": "submission_review"}
                )
                assert created.status_code == 201
                return created.json()["session_id"]

            async def review(client, session, question):
                response = await client.post(
                    f"{_AGENT}/sessions/{session}/runs",
                    json={
                        "skill_version_id": skill_id,
                        "request_id": str(uuid4()),
                        "submission_id": params["submission_id"],
                        "review_question": question,
                        "include_model_review": True,
                    },
                )
                assert response.status_code == 202, response.text
                run = response.json()["run_id"]
                assert await learning.run_once()
                completed = await client.get(f"{_AGENT}/runs/{run}")
                assert completed.json()["state"] == "succeeded", completed.text
                assert completed.json()["result"]["model_hint"]["review_resources"] == [
                    "batch_diagnostics"
                ]
                return run

            session = await conversation()
            first_review = await review(owner, session, "能测一下边界吗？")
            discovery = {"options": [{"title": offer.title, "purpose": "boundary"}], "results": []}
            assert provider_views == [discovery]
            assert (await owner.get(path)).status_code == 404

            body = params | {
                "manifest_sha256": digest,
                "source_execution_confirmed": True,
                "review_run_id": first_review,
            }
            assert (
                await owner.put(path, json=body, headers={"origin": "http://unexpected.test"})
            ).status_code == 403
            assert (
                await owner.put(path, json=body | {"source_execution_confirmed": 1})
            ).status_code == 422
            assert (
                await owner.put(path, json=body | {"manifest_sha256": "0" * 64})
            ).status_code == 404
            assert (await foreign.put(path, json=body)).status_code == 404
            accepted = await owner.put(path, json=body)
            assert accepted.status_code == 202 and accepted.json()["state"] == "queued"
            assert accepted.headers["cache-control"] == "no-store"
            assert (await owner.put(path, json=body)).json() == accepted.json()
            for denied in (foreign, new_session):
                assert (await denied.get(path)).status_code == 404
                assert (await denied.post(f"{path}/cancel")).status_code == 404

            fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
            worker_policy = AdmissionRegistry.model_validate_json(
                (fixtures / "judge-registry.valid.json").read_bytes()
            )
            artifacts = app.state.submissions.artifacts

            async def load(db, pin):
                return await preflight_registered_batch(
                    db, app.state.problems, artifacts, worker_policy, registry, pin
                )

            queue = DiagnosticQueue(app.state.sessions, app.state.identity, load_batch=load)
            claim = await queue.claim()
            assert str(claim.request_id) == accepted.json()["request_id"]
            summary = BatchDiagnosticSummary(
                schema_version=1,
                submission_id=claim.source.submission_id,
                problem_version_id=version,
                purpose="boundary",
                outcome="resource_limit",
            )
            await queue.acknowledge_batch_stopped(claim, summary=summary)
            observed = await owner.get(path)
            assert observed.status_code == 200
            assert observed.json() == accepted.json() | {
                "state": "observed",
                "summary": summary.model_dump(mode="json"),
            }
            await review(owner, session, "刚才的诊断是什么结果？")
            assert provider_views[-1] == discovery | {
                "results": [{"purpose": "boundary", "outcome": "resource_limit"}]
            }
            await review(owner, await conversation(), "有这个对话的诊断吗？")
            assert provider_views[-1] == discovery
            await review(new_session, session, "这个登录会话的诊断是什么？")
            assert provider_views[-1] == discovery
            async with worker_engine.connect() as connection:
                assert not await connection.scalar(
                    text("SELECT has_table_privilege('learning_diagnostic_requests','SELECT')")
                )
                assert not await connection.scalar(
                    text(
                        "SELECT has_function_privilege('kf_oj_platform',"
                        "'kf_review_batch_context(uuid,bigint)','EXECUTE')"
                    )
                )
            await worker_engine.dispose()

            following = f"{_ROOT}/{uuid4()}"
            assert (await owner.put(following, json=body)).status_code == 202
            app.state.agent_learning.diagnostics = DiagnosticQueue(
                app.state.sessions, app.state.identity, batch_offers=()
            )
            assert (await owner.get(f"{_ROOT}/options", params=params)).json() == {"options": []}
            hidden = await owner.get(path)
            assert hidden.status_code == 200
            assert hidden.json()["state"] == "unavailable" and hidden.json().get("summary") is None
            for denied in (foreign, new_session):
                assert (await denied.get(path)).status_code == 404
            assert (await owner.put(f"{_ROOT}/{uuid4()}", json=body)).status_code == 404
            assert (await owner.get(following)).json()["state"] == "queued"
            assert (await owner.post(f"{following}/cancel")).status_code == 204
            assert (await owner.get(following)).json()["state"] == "cancelled"

    asyncio.run(exercise())
