"""Verify the model-review projection with real owned evidence and no provider calls."""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.agent_learning import SubmissionReviewResult
from kf_oj.contracts.learning_model import ModelHint, ReviewModelFacts, SourceExcerptModelFacts
from kf_oj.contracts.submission_evidence import CompilerCorrection, CompilerLocation
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.runtime.model import review_model_facts
from kf_oj.runtime.workflows import LearningWorkflow

ROOT = "/api/v1/learning/agent"
ORIGIN = "http://localhost:5173"


def test_model_review_requires_opt_in_and_only_projects_aggregate_owned_evidence(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        operator = await connect(postgres_url)
        worker_engine = create_async_engine(postgres_url)
        captured = []

        @event.listens_for(worker_engine.sync_engine, "connect")
        def restrict_role(connection, _):
            cursor = connection.cursor()
            cursor.execute("SET ROLE kf_oj_learning_worker")
            cursor.close()

        class LocalModel:
            accepts_source_excerpt = True

            async def hint(self, content):
                if isinstance(content, SourceExcerptModelFacts):
                    captured.append(content.model_dump(mode="json"))
                    return ModelHint(
                        text="手算这一行的边界条件。",
                        model="review-test-double",
                        response_id="fixture-excerpt",
                        latency_ms=0,
                        source_line=content.start_line,
                    )
                assert isinstance(content, ReviewModelFacts)
                captured.append(content.model_dump(mode="json"))
                return ModelHint(
                    text="可以从最小的公开样例入手，核对输入边界并复核修改。",
                    model="review-test-double",
                    response_id="fixture-response",
                    latency_ms=0,
                    review_focus="later_accepted" if content.focus_options else None,
                    review_comparison_case=(
                        "equal"
                        if content.code and content.code.comparison_cases is not None
                        else None
                    ),
                )

        def client():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers={"Origin": ORIGIN}
            )

        async def login(http, name):
            password = secrets.token_urlsafe(24)
            await app.state.identity.create_account(name, password)
            response = await http.post(
                "/api/v1/auth/login", json={"username": name, "password": password}
            )
            assert response.status_code == 200, response.text
            http.headers["x-csrf-token"] = http.cookies["kf_csrf"]
            return await app.state.identity.authenticate(http.cookies["kf_session"])

        async def save_submission(owner, version, verdict, source):
            sample = await app.state.submissions.create(
                owner,
                CreateSubmission(
                    problem_version_id=version,
                    language="cpp20",
                    idempotency_key=uuid4(),
                    source=source,
                ),
            )
            async with operator.transaction():
                await operator.execute("SET LOCAL ROLE kf_oj_coordinator")
                await operator.execute(
                    "UPDATE evaluations SET state='finished', verdict=$1 WHERE id=$2",
                    verdict,
                    sample.evaluation_id,
                )
            return sample

        async def prepare(
            http,
            *,
            opt_in=False,
            use_source=False,
            code_counts=False,
            focus=False,
            control_flow=False,
            comparison_cases=False,
        ):
            versions = (await http.get(ROOT + "/skills")).json()["versions"]
            skill = next(item for item in versions if item["purpose"] == "submission_review")
            saved = await http.put(
                f"{ROOT}/skills/{skill['version_id']}",
                json={
                    "enabled": True,
                    "authorized_tools": [
                        item
                        for item in skill["available_tools"]
                        if item["name"] == "submission_history"
                        or (use_source and item["name"] == "source_evidence")
                    ],
                },
            )
            assert saved.status_code == 200
            session = await http.post(f"{ROOT}/sessions", json={"purpose": "submission_review"})
            assert session.status_code == 201
            endpoint = f"{ROOT}/sessions/{session.json()['session_id']}/runs"
            body = {
                "skill_version_id": skill["version_id"],
                "request_id": str(uuid4()),
                "include_model_review": opt_in,
                "include_source_evidence": use_source,
                "include_model_code_context": code_counts,
                "include_model_evidence_focus": focus,
                "include_model_control_flow": control_flow,
                "include_model_comparison_cases": comparison_cases,
            }
            response = await http.post(endpoint, json=body)
            assert response.status_code == 202, response.text
            return f"{ROOT}/runs/{response.json()['run_id']}", endpoint, body

        worker_sessions = async_sessionmaker(worker_engine, expire_on_commit=False)
        worker = LearningWorker(worker_sessions, LocalModel())
        replacement = LearningWorker(worker_sessions)
        try:
            async with (
                app.router.lifespan_context(app),
                client() as owner,
                client() as foreign,
                client() as empty,
            ):
                actor = await login(owner, "model_review_owner")
                outsider = await login(foreign, "model_review_other")
                await login(empty, "model_review_empty")
                problem = await app.state.problems.import_directory(tmp_path / "import")
                first_source = "// private marker\nint f(int v) { return v; }\nint stable=3;\n"
                second_source = (
                    "// private marker\nint f(int v) { if (v > 0) return v; return 0; }\n"
                    "int stable=3;\nint extra=4;\n"
                )
                wrong = await save_submission(actor, problem, "wrong_answer", first_source)
                passed = await save_submission(actor, problem, "accepted", second_source)
                await save_submission(outsider, problem, "runtime_error", "// foreign marker")

                basic, route, requested = await prepare(owner)
                assert (
                    await owner.post(route, json=requested | {"include_model_review": True})
                ).status_code == 409
                assert (
                    await owner.post(route, json=requested | {"include_model_code_context": True})
                ).status_code == 422
                assert (
                    await owner.post(route, json=requested | {"include_model_evidence_focus": True})
                ).status_code == 422
                assert await worker.run_once()
                assert (await owner.get(basic)).json()["result"]["model_hint"] is None
                assert not captured

                actual, route, body = await prepare(owner, opt_in=True, use_source=True)
                assert (await owner.post(route, json=body)).status_code == 202
                assert (
                    await owner.post(route, json=body | {"include_model_control_flow": True})
                ).status_code == 422
                assert (
                    await owner.post(route, json=body | {"include_model_comparison_cases": True})
                ).status_code == 422
                assert (
                    await owner.post(route, json=body | {"include_model_code_context": True})
                ).status_code == 409
                assert await worker.run_once()
                result = (await owner.get(actual)).json()["result"]
                assert (
                    result["model_status"] == "generated"
                    and result["model_hint"]["model"] == "review-test-double"
                )
                assert {entry["submission_id"] for entry in result["records"]} == {
                    str(wrong.submission_id),
                    str(passed.submission_id),
                }
                assert captured == [
                    {
                        "finished": 2,
                        "accepted": 1,
                        "wrong_answers": 1,
                        "compile_errors": 0,
                        "runtime_errors": 0,
                        "resource_limits": 0,
                        "other_verdicts": 0,
                        "same_version_pairs": 1,
                        "later_accepted_pairs": 1,
                        "truncated": False,
                    }
                ]
                trace = (await owner.get(actual + "/trace")).json()
                assert [step["invocation_version"] for step in trace["steps"]] == [
                    "submission_history-v1",
                    "source_evidence-v1",
                    "responses-review-v3",
                ]
                assert result["model_review_basis"] == "evaluations"
                assert (await foreign.get(actual)).status_code == 404

                extended, _, _ = await prepare(
                    owner, opt_in=True, use_source=True, code_counts=True
                )
                assert await worker.run_once()
                projected = (await owner.get(extended)).json()["result"]
                assert projected["model_review_basis"] == "evaluations_and_code_counts"
                assert captured[-1] == captured[0] | {
                    "code": {
                        "changes": {
                            "regions": 2,
                            "inserted_lines": 1,
                            "deleted_lines": 0,
                            "replaced_before_lines": 1,
                            "replaced_after_lines": 1,
                            "truncated": False,
                        },
                        "compiler_issue": None,
                    }
                }
                steps = (await owner.get(extended + "/trace")).json()["steps"]
                assert steps[-1]["invocation_version"] == "responses-review-code-counts-v3"

                review = SubmissionReviewResult.model_validate(projected)
                latest = next(
                    row for row in review.records if row.submission_id == passed.submission_id
                )
                missing_earlier = review.model_copy(update={"records": (latest,)})
                assert review_model_facts(missing_earlier, include_code_counts=True).code is None
                mismatched = review.model_copy(
                    update={
                        "records": tuple(
                            r.model_copy(update={"evaluation_id": uuid4()}) if r == latest else r
                            for r in review.records
                        )
                    }
                )
                assert review_model_facts(mismatched, include_code_counts=True).code is None
                compile_evidence = missing_earlier.model_copy(
                    update={
                        "records": (latest.model_copy(update={"verdict": "compilation_error"}),),
                        "code": review.code.model_copy(
                            update={
                                "compiler_correction": CompilerCorrection(
                                    location=CompilerLocation(line=2, kind="error"),
                                    kind="missing_semicolon",
                                )
                            }
                        ),
                    }
                )
                assert review_model_facts(
                    compile_evidence, include_code_counts=True
                ).code.model_dump() == {"changes": None, "compiler_issue": "missing_semicolon"}

                barren, _, _ = await prepare(empty, opt_in=True)
                assert await worker.run_once()
                empty_result = (await empty.get(barren)).json()["result"]
                assert empty_result["model_status"] == "insufficient_evidence"
                assert empty_result["model_hint"] is None and len(captured) == 2

                selected, queue, request = await prepare(owner, opt_in=True, focus=True)

                async def interrupted_before_tool(self, parameters, target):
                    raise InterruptedError("fixture worker stopped after the model checkpoint")

                with monkeypatch.context() as interruption:
                    interruption.setattr(
                        LearningWorkflow, "verify_review_evidence", interrupted_before_tool
                    )
                    with pytest.raises(InterruptedError):
                        await worker.run_once()
                assert captured[2:] == [
                    captured[0] | {"focus_options": ["later_accepted", "wrong_answer"]}
                ]
                run_id = UUID(selected.rsplit("/", 1)[-1])
                # Simulate a checkpoint written by the previous review prompt.
                assert (
                    await operator.execute(
                        "UPDATE learning_agent_steps SET invocation_version=$2 "
                        "WHERE run_id=$1 AND invocation_version=$3",
                        run_id,
                        "responses-review-evidence-focus-v2",
                        "responses-review-evidence-focus-v3",
                    )
                    == "UPDATE 1"
                )
                await operator.execute(
                    "UPDATE learning_agent_tasks SET lease_expires_at=clock_timestamp() "
                    "WHERE run_id=$1",
                    run_id,
                )
                # The replacement worker may be online before model configuration is restored.
                assert await replacement.run_once()
                confirmed = (await owner.get(selected)).json()
                assert confirmed["state"] == "succeeded", confirmed
                assert confirmed["result"]["model_status"] == "generated"
                focus = confirmed["result"]["model_focus"]
                assert focus["category"] == "later_accepted"
                assert focus["evidence"]["submission_id"] == str(passed.submission_id)
                assert focus["evidence"]["evaluation_id"] == str(passed.evaluation_id)
                trace = (await owner.get(selected + "/trace")).json()
                assert trace["attempts"] == 2
                assert [step["invocation_version"] for step in trace["steps"]] == [
                    "submission_history-v1",
                    "responses-review-evidence-focus-v2",
                    "submission_history-v1",
                ]
                replay = await owner.post(queue, json=request)
                assert replay.status_code == 202 and replay.json() == confirmed
                assert len(captured) == 3

                new_run, _, _ = await prepare(owner, opt_in=True)
                assert await replacement.run_once()
                fallback = (await owner.get(new_run)).json()["result"]
                assert fallback["model_status"] == "unavailable" and fallback["model_hint"] is None
                trace = (await owner.get(new_run + "/trace")).json()
                assert all(step["kind"] != "model" for step in trace["steps"])
                assert "model_usage" not in trace
                assert len(captured) == 3

                measured, _, _ = await prepare(
                    owner, opt_in=True, use_source=True, code_counts=True, control_flow=True
                )
                assert await worker.run_once()
                data = (await owner.get(measured)).json()["result"]
                flow = {
                    "method": "control-keywords-v1",
                    "before": {"branch_keywords": 0, "loop_keywords": 0, "return_keywords": 1},
                    "after": {"branch_keywords": 1, "loop_keywords": 0, "return_keywords": 2},
                }
                assert data["model_review_basis"] == "evaluations_and_control_flow"
                assert data["code"]["comparison"]["control_flow"] == flow
                assert captured[3:] == [
                    captured[1] | {"code": captured[1]["code"] | {"control_flow": flow}}
                ]
                trace = (await owner.get(measured + "/trace")).json()["steps"]
                assert trace[-1]["invocation_version"] == "responses-review-control-flow-v3"

                old_condition = (
                    "int f(int private_value) {\n"
                    "if (private_value < 2) {\nreturn 1;\n}\n"
                    "return 0;\n}\n"
                )
                await save_submission(actor, problem, "wrong_answer", old_condition)
                await save_submission(
                    actor, problem, "accepted", old_condition.replace(" < 2", " <= 2")
                )
                cases, _, _ = await prepare(
                    owner, opt_in=True, use_source=True, code_counts=True, comparison_cases=True
                )
                assert await worker.run_once()
                result = (await owner.get(cases)).json()["result"]
                assert result["model_review_basis"] == "evaluations_and_comparison_cases"
                assert result["model_hint"]["review_comparison_case"] == "equal"
                assert result["code"]["comparison"]["boundary_changes"][0]["changed_cases"] == [
                    "equal"
                ]
                assert captured[-1]["code"]["comparison_cases"] == {
                    "less": 0,
                    "equal": 1,
                    "greater": 0,
                }
                assert "private_value" not in json.dumps(captured[-1])
                assert "before_operator" not in json.dumps(captured[-1])
                unshared = review_model_facts(
                    SubmissionReviewResult.model_validate(result), include_code_counts=True
                )
                assert "comparison_cases" not in unshared.model_dump(mode="json")["code"]

                chat_root = await owner.post(f"{ROOT}/sessions", json={"purpose": "guided_help"})
                assert chat_root.status_code == 201
                root_id = chat_root.json()["session_id"]
                review_session = await owner.post(
                    f"{ROOT}/sessions",
                    json={"purpose": "submission_review", "conversation_session_id": root_id},
                )
                assert review_session.status_code == 201
                chat_route = f"{ROOT}/sessions/{review_session.json()['session_id']}/runs"
                typed = {
                    "skill_version_id": request["skill_version_id"],
                    "request_id": str(uuid4()),
                    "submission_id": str(passed.submission_id),
                    "review_question": "复盘这个提交的边界情况。",
                    "include_model_review": True,
                }
                assert (
                    await owner.post(chat_route, json=typed | {"include_model_review": False})
                ).status_code == 422
                assert (
                    await owner.post(chat_route, json=typed | {"include_source_evidence": True})
                ).status_code == 422
                review_receipt = await owner.post(chat_route, json=typed)
                assert review_receipt.status_code == 202, review_receipt.text
                assert review_receipt.json()["review_question"] == typed["review_question"]
                assert await worker.run_once()
                review_id = review_receipt.json()["run_id"]
                readback = (await owner.get(f"{ROOT}/runs/{review_id}")).json()["result"]
                assert readback["code"] is None and readback["model_status"] == "generated"
                assert readback["anchor_submission_id"] == typed["submission_id"]
                assert captured[-1]["question"] == typed["review_question"]
                assert "text" not in captured[-1] and "code" not in captured[-1]

                with_excerpt = typed | {
                    "request_id": str(uuid4()),
                    "review_question": "帮我解释选中的代码行。",
                    "include_source_evidence": True,
                    "model_source_excerpt": {"start_line": 2, "end_line": 2},
                }
                excerpt_receipt = await owner.post(chat_route, json=with_excerpt)
                assert excerpt_receipt.status_code == 202, excerpt_receipt.text
                assert await worker.run_once()
                excerpt_id = excerpt_receipt.json()["run_id"]
                reviewed = (await owner.get(f"{ROOT}/runs/{excerpt_id}")).json()["result"]
                assert (
                    reviewed["code"] is None and reviewed["model_review_basis"] == "source_excerpt"
                )
                assert reviewed["model_hint"]["source_line"] == 2
                assert captured[-1] == {
                    "question": with_excerpt["review_question"],
                    "start_line": 2,
                    "end_line": 2,
                    "language": "cpp20",
                    "text": second_source.splitlines()[1],
                }
                grouped = (await owner.get(f"{ROOT}/sessions/{root_id}/conversation")).json()
                assert [(row["run_id"], row["question"]) for row in grouped["runs"]] == [
                    (review_id, typed["review_question"]),
                    (excerpt_id, with_excerpt["review_question"]),
                ]
                assert (await foreign.get(f"{ROOT}/runs/{excerpt_id}")).status_code == 404
        finally:
            await worker_engine.dispose()
            await operator.close()

    asyncio.run(exercise())
