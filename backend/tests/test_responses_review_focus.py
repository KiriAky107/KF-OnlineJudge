"""Check Responses output validation and provider failure classification."""

import asyncio
import json

import httpx
import pytest

from kf_oj.contracts.learning_model import (
    GuidedChatModelFacts,
    ModelHintUnavailable,
    ReviewCodeFacts,
    ReviewComparisonCases,
    ReviewModelFacts,
)
from kf_oj.infra.responses_model import ResponsesLearningHint


def test_deepseek_chat_keeps_the_default_thinking_and_full_stream(monkeypatch):
    original_client = httpx.AsyncClient
    requests = []
    previews = {}
    answer = "检查区间的两个端点。" * 1000
    reasoning = "先考虑搜索区间。" * 1200

    def output(text):
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        result = {
            "id": "fixture-response",
            "model": "deepseek-flash",
            "object": "response",
            "status": "completed",
            "output": [output("检查区间的两个端点。")],
        }
        if not body.get("stream"):
            return httpx.Response(200, json=result)
        result["output"] = [output(answer)]
        events = (
            {"type": "response.reasoning_text.delta", "delta": reasoning},
            {"type": "response.output_text.delta", "delta": answer},
            {"type": "response.completed", "response": result},
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    def local_client(**kwargs):
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_client)
    adapter = ResponsesLearningHint(
        "https://api.deepseek.com", "deepseek-flash", None, reasoning_effort="none"
    )

    async def publish(channel, text):
        previews[channel] = text

    async def run():
        short_hint = await adapter.hint("binary_search")
        chat_hint = await adapter.stream_hint(
            GuidedChatModelFacts(question="二分查找怎么写？"), publish
        )
        assert short_hint.reasoning is None
        assert chat_hint.text == answer
        assert chat_hint.reasoning == reasoning

    asyncio.run(run())
    assert previews == {"answer": answer, "reasoning": reasoning}
    short, chat = requests
    assert short["reasoning"] == {"effort": "none"}
    assert short["max_output_tokens"] == 128
    assert "reasoning" not in chat and "max_output_tokens" not in chat
    assert "答案简洁" not in chat["instructions"]


@pytest.mark.parametrize(
    "selection",
    [
        {"text": "核对编译失败。", "focus": "compilation_error"},
        {"text": "核对错误答案。", "focus": "wrong_answer", "submission_id": "proposed-id"},
        {"text": "核对取值关系。", "focus": None, "comparison_case": "greater"},
    ],
)
def test_review_rejects_unoffered_categories_and_record_identifiers(monkeypatch, selection):
    original_client = httpx.AsyncClient
    comparison_case = "comparison_case" in selection
    reported = {"input_tokens": 19, "output_tokens": 8, "total_tokens": 27}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert json.loads(body["input"])["focus_options"] == ["wrong_answer"]
        if comparison_case:
            assert json.loads(body["input"])["code"]["comparison_cases"] == {
                "less": 0,
                "equal": 1,
                "greater": 0,
            }
        assert body["max_output_tokens"] == 128 and body["store"] is False
        assert "最多45字" in body["instructions"] and "不复述计数" in body["instructions"]
        assert "语言、提交是否相邻、改动行号和实际输出需要具体记录" in body["instructions"]
        assert "later_accepted 只复核同版本后次通过的那条评测" in body["instructions"]
        return httpx.Response(
            200,
            json={
                "id": "fixture-response",
                "model": "fixture-model",
                "object": "response",
                "status": "completed",
                "usage": reported,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(selection)}],
                    }
                ],
            },
        )

    def local_client(**kwargs):
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_client)
    adapter = ResponsesLearningHint("http://127.0.0.1/v1", "fixture-model", None)
    content = ReviewModelFacts(
        finished=1,
        accepted=0,
        wrong_answers=1,
        compile_errors=0,
        runtime_errors=0,
        resource_limits=0,
        other_verdicts=0,
        same_version_pairs=0,
        later_accepted_pairs=0,
        truncated=False,
        focus_options=("wrong_answer",),
        code=(
            ReviewCodeFacts(comparison_cases=ReviewComparisonCases(less=0, equal=1, greater=0))
            if comparison_case
            else None
        ),
    )
    with pytest.raises(ModelHintUnavailable) as rejected:
        asyncio.run(adapter.hint(content))
    assert rejected.value.code == "model_invalid_response"
    assert rejected.value.usage is not None
    assert rejected.value.usage.model_dump() == reported


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "model_timeout"),
        (429, "model_rate_limited"),
        (503, "model_provider_rejected"),
        ("truncated", "model_output_limit"),
    ],
)
def test_model_transport_failures_have_distinct_codes(monkeypatch, status, expected):
    original_client = httpx.AsyncClient

    def respond(request):
        if status is None:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if status == "truncated":
            return httpx.Response(
                200,
                json={
                    "object": "response",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"content": [{"text": "private provider details"}]}],
                },
            )
        return httpx.Response(status, text="private provider details")

    def local_client(**kwargs):
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_client)
    adapter = ResponsesLearningHint("http://127.0.0.1/v1", "fixture-model", None)
    with pytest.raises(ModelHintUnavailable) as rejected:
        asyncio.run(adapter.hint("binary_search"))
    assert rejected.value.code == expected
    assert "private provider details" not in str(rejected.value)
