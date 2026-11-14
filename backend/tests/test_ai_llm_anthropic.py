"""AnthropicLLM adapter with a mocked SDK client (no network)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from app.ai.llm import AnthropicLLM, LLMError, LLMRefusal, LLMRequest, LLMUnavailable
from app.ai.prompts import STABLE_SYSTEM_PROMPT
from app.ai.tools import TOOL_DEFINITIONS


def _resp(stop: str, content: list, inp: int = 100, out: int = 20):
    return NS(
        stop_reason=stop,
        content=content,
        _request_id="req_1",
        usage=NS(
            input_tokens=inp, output_tokens=out, cache_read_input_tokens=50, cache_creation_input_tokens=0
        ),
    )


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _req() -> LLMRequest:
    return LLMRequest(
        system_stable=STABLE_SYSTEM_PROMPT,
        system_dynamic="<context>x</context>",
        messages=[{"role": "user", "content": "Сколько стоит стрижка?"}],
        tools=TOOL_DEFINITIONS,
        language="ru",
    )


def test_tool_loop_passes_thinking_back_and_returns_text():
    thinking = NS(type="thinking", thinking="", signature="sig")
    tool_use = NS(type="tool_use", id="toolu_1", name="list_services", input={})
    first = _resp("tool_use", [thinking, tool_use])
    second = _resp("end_turn", [NS(type="text", text="Стрижка стоит 5000 ₸.")])
    client = _Client([first, second])
    executed = []

    def execute(name, args):
        executed.append((name, args))
        return {"ok": True, "services": [{"title": "Стрижка", "price": "5000"}]}

    llm = AnthropicLLM(client=client, model="claude-opus-5-5", effort="low")
    result = llm.run(_req(), execute)

    assert result.text == "Стрижка стоит 5000 ₸." and executed == [("list_services", {})]
    assert result.iterations == 2 and result.input_tokens == 300 and result.output_tokens == 40
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "low"} and call["tool_choice"] == {"type": "auto"}
    assert "temperature" not in call and "budget_tokens" not in str(call["thinking"])
    assert (
        call["system"][0]["cache_control"] == {"type": "ephemeral"}
        and "cache_control" not in call["system"][1]
    )
    assert all(t["strict"] for t in call["tools"])
    followup = client.calls[1]["messages"]
    assert followup[1] == {"role": "assistant", "content": first.content}  # unchanged, incl. thinking block
    result_block = followup[2]["content"][0]
    assert result_block["type"] == "tool_result" and result_block["tool_use_id"] == "toolu_1"
    assert result_block["is_error"] is False and "5000" in result_block["content"]


def test_tool_error_is_flagged():
    client = _Client(
        [
            _resp("tool_use", [NS(type="tool_use", id="t1", name="get_booking", input={"booking": "1"})]),
            _resp("end_turn", [NS(type="text", text="Не нашёл запись.")]),
        ]
    )
    AnthropicLLM(client=client).run(_req(), lambda n, a: {"ok": False, "error": "NOT_FOUND"})
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_and_truncation():
    with pytest.raises(LLMRefusal):
        AnthropicLLM(client=_Client([_resp("refusal", [])])).run(_req(), lambda n, a: {"ok": True})
    executed = []
    trunc = _resp("max_tokens", [NS(type="tool_use", id="t1", name="create_booking", input={"slot_id": "a"})])
    with pytest.raises(LLMError):
        AnthropicLLM(client=_Client([trunc])).run(_req(), lambda n, a: executed.append(n) or {"ok": True})
    assert executed == []  # a truncated tool call is never executed


def test_api_errors_are_typed():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with pytest.raises(LLMUnavailable):
        AnthropicLLM(client=_Client([anthropic.APIConnectionError(request=req)])).run(_req(), lambda n, a: {})
    bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=req), body=None)
    with pytest.raises(LLMError) as exc:
        AnthropicLLM(client=_Client([bad])).run(_req(), lambda n, a: {})
    assert not isinstance(exc.value, LLMUnavailable)


def test_loop_limit():
    loop = [
        _resp("tool_use", [NS(type="tool_use", id=f"t{i}", name="list_services", input={})]) for i in range(3)
    ]
    with pytest.raises(LLMError):
        AnthropicLLM(client=_Client(loop), max_iterations=3).run(_req(), lambda n, a: {"ok": True})
