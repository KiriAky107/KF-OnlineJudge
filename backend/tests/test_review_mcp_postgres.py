"""Exercise a model-selected MCP lookup and charge both provider requests."""

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from conftest import connect
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.infra.responses_model import ResponsesLearningHint
from kf_oj.modules.learning.agent_model_budget import AgentModelLimits
from kf_oj.modules.learning.agent_tool_policy import (
    REVIEW_EVALUATION_READS_PER_WINDOW,
    reserve_review_evaluation_read,
)
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.submissions.public import owned_review_evaluation_category

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_review_mcp_returns_only_requested_evidence_with_shared_budget(
    postgres_url, tmp_path, monkeypatch, capsys
):
    original_client = httpx.AsyncClient
    requests = []
    selected_tools = ("problem", "code")
    before_tool_reply = None
    usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}

    async def model_reply(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["tool_choice"] == "auto":
            if before_tool_reply is not None:
                await before_tool_reply()
            output = [
                {
                    "type": "function_call",
                    "name": name,
                    "arguments": "{}",
                    "call_id": f"call_{name}",
                }
                for name in selected_tools
            ]
        else:
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "用题面的样例核对改动。"}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(requests)}",
                "object": "response",
                "status": "completed",
                "model": "review-mcp-fixture",
                "usage": usage,
                "output": output,
            },
        )

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(model_reply))
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def exercise():
        nonlocal before_tool_reply, selected_tools
        app, engine, api_module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(api_module, "build_engine", lambda _: engine)
        worker_engine = create_async_engine(postgres_url)

        @event.listens_for(worker_engine.sync_engine, "connect")
        def role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        worker = LearningWorker(
            async_sessionmaker(worker_engine, expire_on_commit=False),
            ResponsesLearningHint(
                "http://127.0.0.1/v1", "review-mcp-fixture", None, review_mcp=True
            ),
            model_limits=AgentModelLimits(account_per_hour=3, service_per_hour=3),
        )
        operator = await connect(postgres_url)
        try:
            async with (
                app.router.lifespan_context(app),
                original_client(
                    transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
                ) as browser,
            ):
                password = secrets.token_urlsafe(24)
                owner = await app.state.identity.create_account("mcp_review_owner", password)
                login = await browser.post(
                    "/api/v1/auth/login",
                    json={"username": "mcp_review_owner", "password": password},
                )
                assert login.status_code == 200
                browser.headers["x-csrf-token"] = browser.cookies["kf_csrf"]
                version = await app.state.problems.import_directory(tmp_path / "import")
                for verdict, number in (("wrong_answer", 1), ("time_limit_exceeded", 0)):
                    submission = await app.state.submissions.create(
                        owner,
                        CreateSubmission(
                            problem_version_id=version,
                            language="cpp20",
                            source=f"int main() {{ return {number}; }} // private code marker\n",
                            idempotency_key=uuid4(),
                        ),
                    )
                    async with operator.transaction():
                        await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                        await operator.execute(
                            "UPDATE evaluations SET state='finished',verdict=$1 WHERE id=$2",
                            verdict,
                            submission.evaluation_id,
                        )

                catalog = (await browser.get(f"{ROOT}/skills")).json()["versions"]
                skill = next(item for item in catalog if item["purpose"] == "submission_review")
                configured = await browser.put(
                    f"{ROOT}/skills/{skill['version_id']}",
                    json={"enabled": True, "authorized_tools": skill["available_tools"]},
                )
                assert configured.status_code == 200
                created = await browser.post(
                    f"{ROOT}/sessions", json={"purpose": "submission_review"}
                )
                session = created.json()["session_id"]
                question = "复盘我选中的提交。"

                async def review(using=worker, *, expire_claim=False):
                    nonlocal before_tool_reply
                    receipt = await browser.post(
                        f"{ROOT}/sessions/{session}/runs",
                        json={
                            "skill_version_id": skill["version_id"],
                            "request_id": str(uuid4()),
                            "submission_id": str(submission.submission_id),
                            "review_question": question,
                            "include_model_review": True,
                        },
                    )
                    assert receipt.status_code == 202, receipt.text
                    identifier = receipt.json()["run_id"]

                    async def expire_lease():
                        changed = await operator.execute(
                            "UPDATE learning_agent_tasks "
                            "SET lease_expires_at=clock_timestamp()-interval '1 second' "
                            "WHERE run_id=$1",
                            UUID(identifier),
                        )
                        assert changed == "UPDATE 1"

                    before_tool_reply = expire_lease if expire_claim else None
                    try:
                        assert await using.run_once()
                    finally:
                        before_tool_reply = None
                    return identifier, (await browser.get(f"{ROOT}/runs/{identifier}")).json()[
                        "result"
                    ]

                run_id, result = await review()
                assert result["model_status"] == "generated"
                assert result["model_hint"]["review_resources"] == ["problem", "code"]
                assert result["model_hint"]["usage"]["total_tokens"] == 30
                assert len(requests) == 2
                assert json.loads(requests[0]["input"]) == {"question": question}
                assert requests[0]["store"] is False
                assert {item["name"] for item in requests[0]["tools"]} == {
                    "problem",
                    "evaluations",
                    "code",
                }
                fetched = {
                    item["call_id"]: json.loads(item["output"])
                    for item in requests[1]["input"]
                    if item.get("type") == "function_call_output"
                }
                assert set(fetched) == {"call_problem", "call_code"}
                assert fetched["call_problem"]["title"] == "Synthetic problem"
                assert fetched["call_problem"]["statement"]
                assert fetched["call_code"]["changes"]["replaced_after_lines"] == 1
                assert "private code marker" not in json.dumps(requests)
                assert str(submission.submission_id) not in json.dumps(requests)
                assert requests[1]["tool_choice"] == "none"

                later_id, later = await review()
                assert later["model_status"] == "budget_exhausted"
                assert len(requests) == 3
                attempts = await operator.fetch(
                    "SELECT provider_requests, reported_usage FROM learning_agent_model_attempts "
                    "WHERE run_id IN ($1,$2) ORDER BY provider_requests DESC",
                    UUID(run_id),
                    UUID(later_id),
                )
                assert [row["provider_requests"] for row in attempts] == [2, 1]
                assert [json.loads(row["reported_usage"])["total_tokens"] for row in attempts] == [
                    30,
                    15,
                ]
                default_worker = LearningWorker(
                    async_sessionmaker(worker_engine, expire_on_commit=False),
                    ResponsesLearningHint(
                        "http://127.0.0.1/v1",
                        "review-mcp-fixture",
                        None,
                        review_mcp=True,
                        record_usage=False,
                    ),
                )
                unlimited_id, unlimited = await review(default_worker)
                assert len(requests) == 5
                assert unlimited["model_status"] == "generated"
                assert unlimited["model_hint"]["usage"] is None
                assert (
                    await operator.fetchval(
                        "SELECT reported_usage FROM learning_agent_model_attempts WHERE run_id=$1",
                        UUID(unlimited_id),
                    )
                    is None
                )

                # A new provider request or chat cannot refresh the shared evidence budget.
                selected_tools = ("evaluations",)
                safe_summary = {
                    "finished": 2,
                    "accepted": 0,
                    "wrong_answers": 1,
                    "compile_errors": 0,
                    "runtime_errors": 0,
                    "resource_limits": 1,
                    "other_verdicts": 0,
                    "same_version_pairs": 1,
                    "later_accepted_pairs": 0,
                    "truncated": False,
                    "selected_evaluation": {"schema_version": 1, "category": "resource_limit"},
                }
                for _ in range(REVIEW_EVALUATION_READS_PER_WINDOW):
                    _, summary = await review(default_worker)
                    assert summary["model_hint"]["review_resources"] == ["evaluations"]
                    selected = requests[-1]["input"][-1]
                    assert selected["type"] == "function_call_output"
                    assert json.loads(selected["output"]) == safe_summary
                    assert str(submission.evaluation_id) not in selected["output"]
                extra_session = await browser.post(
                    f"{ROOT}/sessions", json={"purpose": "submission_review"}
                )
                assert extra_session.status_code == 201
                session = extra_session.json()["session_id"]
                _, limited = await review(default_worker)
                assert limited["model_hint"]["review_resources"] == []
                assert json.loads(requests[-1]["input"][-1]["output"]) == {"unavailable": True}

                other = await app.state.identity.create_account(
                    "mcp_review_other", secrets.token_urlsafe(24)
                )
                sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
                current = datetime.now(UTC)
                selection = {
                    "submission_id": submission.submission_id,
                    "evaluation_id": submission.evaluation_id,
                    "problem_version_id": version,
                    "as_of": current,
                }
                async with sessions.begin() as db:
                    assert await owned_review_evaluation_category(db, other, **selection) is None
                    assert (
                        await owned_review_evaluation_category(
                            db, owner, **(selection | {"problem_version_id": uuid4()})
                        )
                        is None
                    )
                    assert (
                        await owned_review_evaluation_category(
                            db, owner, **(selection | {"evaluation_id": uuid4()})
                        )
                        is None
                    )
                    assert (
                        await owned_review_evaluation_category(
                            db, owner, **(selection | {"as_of": current - timedelta(hours=1)})
                        )
                        is None
                    )
                    assert await reserve_review_evaluation_read(db, other.account_id, version)
                    assert not await db.scalar(
                        text("SELECT has_any_column_privilege('problem_cases', 'SELECT')")
                    )
                    assert not await db.scalar(
                        text("SELECT has_any_column_privilege('case_results', 'SELECT')")
                    )

                await operator.execute(
                    "UPDATE learning_review_evaluation_budgets "
                    "SET window_started_at=now()-interval '1 hour' WHERE owner_id=$1",
                    owner.account_id,
                )
                _, reset = await review(default_worker)
                assert reset["model_hint"]["review_resources"] == ["evaluations"]
                assert json.loads(requests[-1]["input"][-1]["output"]) == safe_summary

                request_count = len(requests)
                _, expired = await review(default_worker, expire_claim=True)
                assert expired is None
                assert len(requests) == request_count + 1
                assert (
                    await operator.fetchval(
                        "SELECT reads FROM learning_review_evaluation_budgets "
                        "WHERE owner_id=$1 AND problem_version_id=$2",
                        owner.account_id,
                        version,
                    )
                    == 1
                )
        finally:
            await operator.close()
            await worker_engine.dispose()

    asyncio.run(exercise())
    captured = capsys.readouterr()
    assert "private code marker" not in captured.out + captured.err
