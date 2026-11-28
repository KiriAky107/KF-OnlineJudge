"""Verify exact on-wire input bounds and preserve the current question."""

import asyncio
import json

import httpx
import pytest

from kf_oj.contracts.learning_model import (
    GuidedChatModelFacts,
    LearningChatTurn,
    ModelHintUnavailable,
    ReviewModelFacts,
    SourceExcerptModelFacts,
)
from kf_oj.core.settings import Settings
from kf_oj.infra.responses_model import ResponsesLearningHint


def _reply(output: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "response",
            "status": "completed",
            "id": "context-response",
            "model": "context-fixture",
            "output": output,
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        },
    )


def _answer(text: str = "核对公开样例的边界。") -> list[dict]:
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    ]


def test_compaction_preserves_current_question_and_authorized_excerpt(monkeypatch):
    settings = Settings(_env_file=None)
    assert settings.learning_model_account_calls_per_hour is None
    assert settings.learning_model_service_calls_per_hour is None
    assert settings.learning_model_record_usage is False
    context_bytes = 8192
    original = httpx.AsyncClient
    sent = []

    def provider(request):
        sent.append(request.content)
        return _reply(_answer())

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw)
    )
    source = SourceExcerptModelFacts(
        start_line=2, end_line=2, language="cpp20", text="return values[i];"
    )
    facts = GuidedChatModelFacts(
        question="为什么最后一次比较漏了？",
        hint_level="L3",
        problem_statement="公开约束：" + "边界条件与样例" * 500,
        history=(
            LearningChatTurn(question="以前的问题", answer="旧答案要点。" * 700),
            LearningChatTurn(question="继续说明", answer="更多旧上下文。" * 700),
            LearningChatTurn(question="最近问题", answer="最近答案。" * 700),
        ),
        source_excerpt=source,
    )
    adapter = ResponsesLearningHint(
        "http://127.0.0.1/v1",
        "context-fixture",
        None,
        context_bytes=context_bytes,
        record_usage=False,
    )
    hint = asyncio.run(adapter.hint(facts))
    assert hint.usage is None
    assert len(sent) == 1 and len(sent[0]) <= context_bytes
    payload = json.loads(sent[0])
    compact = json.loads(payload["input"])
    assert compact["context_compacted"] is True
    assert compact["question"] == facts.question and compact["hint_level"] == "L3"
    assert compact["source_excerpt"] == source.model_dump(mode="json")
    assert compact["history"][-1]["question"] == "最近问题"
    assert len(compact["problem_statement"]) < len(facts.problem_statement)
    assert compact["problem_statement"].startswith("公开约束：")


def test_mcp_continuation_compacts_public_context_before_sending(monkeypatch):
    original = httpx.AsyncClient
    sent = []
    answer = "核对公开样例的边界。\n" * 2500 + "这是答复的最后一段。"

    def provider(request):
        sent.append(request.content)
        if len(sent) == 1:
            return _reply(
                [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "先查看题面。"}],
                        "summary": [{"type": "summary_text", "text": "查阅。"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_public",
                        "name": "problem",
                        "arguments": "{}",
                    },
                ]
            )
        return _reply(
            [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "核对边界。"}],
                },
                *_answer(answer),
            ]
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw)
    )
    adapter = ResponsesLearningHint(
        "https://api.deepseek.com/v1",
        "context-fixture",
        None,
        review_mcp=True,
        context_bytes=8192,
    )
    facts = ReviewModelFacts(
        question="复盘此题。",
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
    )

    async def read():
        return json.dumps(
            {"title": "公开测试题", "statement": "公开题面。" * 1000, "statement_truncated": False},
            ensure_ascii=False,
        )

    async def proceed(resources):
        assert resources == ("problem",)
        return True

    hint = asyncio.run(adapter.review_with_tools(facts, {"problem": read}, proceed))
    assert hint.review_resources == ("problem",)
    assert hint.text == answer
    assert hint.reasoning == "先查看题面。\n\n核对边界。"
    assert hint.reasoning_kind == "text"
    assert len(sent) == 2 and all(len(item) <= 8192 for item in sent)
    first = json.loads(sent[0])
    assert "reasoning" not in first
    assert "max_output_tokens" not in first
    continuation = json.loads(sent[-1])
    assert "max_output_tokens" not in continuation
    assert json.loads(continuation["input"][0]["content"])["question"] == facts.question
    public_context = json.loads(continuation["input"][-1]["output"])
    assert public_context["title"] == "公开测试题"
    assert public_context["statement_truncated"] and "中间内容已压缩" in public_context["statement"]


@pytest.mark.parametrize("use_tool", [False, True])
def test_practice_overview_reaches_model_only_after_it_requests_the_tool(monkeypatch, use_tool):
    original = httpx.AsyncClient
    inputs = []
    reads = []
    title = "我的合成题目"
    call = {
        "type": "function_call",
        "call_id": "practice_call",
        "name": "recent_practice",
        "arguments": "{}",
    }

    def provider(request):
        assert len(request.content) <= 8192
        body = json.loads(request.content)
        inputs.append(body["input"])
        if body["tool_choice"] == "auto":
            assert title not in request.content.decode()
            assert [item["name"] for item in body["tools"]] == ["recent_practice"]
            assert "当前问题要求不读取做题记录时，直接回答" in body["instructions"]
            if use_tool:
                return _reply([call])
        else:
            assert body["tool_choice"] == "none"
            assert body["input"][-1]["type"] == "function_call_output"
            assert json.loads(body["input"][-1]["output"])["recent_practice"][0]["title"] == title
        return _reply(_answer())

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw)
    )
    facts = GuidedChatModelFacts(
        question="说说我的练习进度。" if use_tool else "别查我的练习进度，用猫娘口吻讲二分。",
        recent_practice=(
            {"title": title, "difficulty": "简单", "difficulty_score": 100, "accepted": True},
        ),
        history=(LearningChatTurn(question="旧问题", answer="更早的内容。" * 1600),),
        problem_statement="公开题面与约束。" * 500,
    )

    async def read():
        reads.append(True)
        return json.dumps({"recent_practice": [facts.recent_practice[0].model_dump()]})

    async def proceed(resources):
        assert resources == ("recent_practice",)
        return True

    async def publish(_channel, _text):
        pass

    adapter = ResponsesLearningHint(
        "https://api.deepseek.com/v1", "context-fixture", None, review_mcp=True, context_bytes=8192
    )
    hint = asyncio.run(adapter.chat_with_tools(facts, publish, read, proceed))
    assert len(reads) == int(use_tool)
    assert len(inputs) == 1 + int(use_tool)
    assert hint.review_resources == (("recent_practice",) if use_tool else ())
    user_input = inputs[-1][0]["content"] if use_tool else inputs[-1]
    context = json.loads(user_input)
    assert context["question"] == facts.question and context["recent_practice"] == []
    assert context["context_compacted"]


def test_practice_continuation_fits_long_titles_without_changing_the_question(monkeypatch):
    budget = 8192
    question = "请结合最近练习回答。" + "练习背景" * 290
    practice = [
        {
            "title": f"{index}" + "𠮷" * 159,
            "difficulty": "中等",
            "difficulty_score": 1500,
            "accepted": bool(index % 2),
        }
        for index in range(8)
    ]
    facts = GuidedChatModelFacts(question=question, recent_practice=tuple(practice))
    original = httpx.AsyncClient
    requests = []

    def provider(request):
        assert len(request.content) <= budget
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert json.loads(body["input"])["recent_practice"] == []
            return _reply(
                [
                    {
                        "type": "function_call",
                        "call_id": "practice_call",
                        "name": "recent_practice",
                        "arguments": "{}",
                    }
                ]
            )
        assert json.loads(body["input"][0]["content"])["question"] == question
        reply = body["input"][-1]
        assert reply["call_id"] == "practice_call"
        context = json.loads(reply["output"])
        retained = context["recent_practice"]
        assert 0 < len(retained) < len(practice)
        assert retained == practice[: len(retained)]
        assert context["recent_practice_truncated"] is True
        assert "max_output_tokens" not in body
        return _reply(_answer("从返回的近期记录看，可以先复练边界条件。"))

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw)
    )
    adapter = ResponsesLearningHint(
        "http://127.0.0.1/v1", "context-fixture", None, review_mcp=True, context_bytes=budget
    )

    async def read():
        return json.dumps({"recent_practice": practice}, ensure_ascii=False)

    async def proceed(resources):
        assert resources == ("recent_practice",)
        return True

    async def publish(_channel, _value):
        pass

    hint = asyncio.run(adapter.chat_with_tools(facts, publish, read, proceed))
    assert hint.review_resources == ("recent_practice",)
    assert len(requests) == 2


@pytest.mark.parametrize("allowed", [False, True])
def test_oversized_provider_reasoning_does_not_prevent_an_authorized_answer(monkeypatch, allowed):
    original = httpx.AsyncClient
    budget = 8192
    question = "先读最近的练习，再提出一个复习建议。" + "已知背景。" * 220
    reasoning = "  " + "已有练习需要按记录核对。\n" * 750 + "  "
    final_reasoning = "根据结果建议先复习边界条件。"
    answer = "可以先用最近一道边界题核对循环终止条件。"
    rows = [
        {"title": str(i) + "𠮷" * 159, "difficulty": "中等", "accepted": False} for i in range(8)
    ]
    sent = []
    callbacks = []

    def provider(request):
        assert len(request.content) <= budget
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return _reply(
                [
                    {
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": reasoning}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "practice_call",
                        "name": "recent_practice",
                        "arguments": "{}",
                    },
                ]
            )
        assert len(callbacks) == 2 and callbacks[-1] == ("recent_practice",)
        assert "tools" not in body and "tool_choice" not in body
        assert body["store"] is False and "max_output_tokens" not in body
        user, tool_result = body["input"]
        assert user["role"] == tool_result["role"] == "user"
        assert json.loads(user["content"])["question"] == question
        context = json.loads(tool_result["content"])
        assert context["authorized_site_resource"] == "recent_practice"
        evidence = context["evidence"]
        assert "recent_practice_truncated" in evidence
        retained = evidence["recent_practice"]
        assert 0 < len(retained) < 8 and retained == rows[: len(retained)]
        return _reply(
            [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": final_reasoning}],
                },
                *_answer(answer),
            ]
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw)
    )
    adapter = ResponsesLearningHint(
        "http://127.0.0.1/v1", "context-fixture", None, review_mcp=True, context_bytes=budget
    )

    async def read():
        callbacks.append("read")
        return json.dumps({"recent_practice": rows}, ensure_ascii=False)

    async def proceed(resources):
        callbacks.append(resources)
        return allowed

    async def publish(_channel, _value):
        pass

    def run():
        return asyncio.run(
            adapter.chat_with_tools(GuidedChatModelFacts(question=question), publish, read, proceed)
        )

    if not allowed:
        with pytest.raises(ModelHintUnavailable) as denial:
            run()
        assert denial.value.code == "model_budget_exhausted"
        assert len(sent) == 1
    else:
        hint = run()
        assert len(sent) == 2
        assert hint.text == answer and hint.review_resources == ("recent_practice",)
        assert hint.reasoning == reasoning + "\n\n" + final_reasoning
    assert callbacks == ["read", ("recent_practice",)]
