"""Swappable LLM adapters for the client-facing agent.

- `AnthropicLLM`: Claude via the official `anthropic` SDK with a manual tool-use loop (strict tool schemas,
  adaptive thinking, low/medium effort for chat latency, prompt caching of the stable prefix).
- `MockLLM`: deterministic rule-based agent (app.ai.mock_llm) that calls the same tools; used in dev/tests.

Select with settings.llm_provider ("anthropic" | "mock"). `get_llm()` returns a process-wide instance.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from app.config import get_settings

log = logging.getLogger("ai.llm")

ToolExecutor = Callable[[str, Any], dict[str, Any]]


class LLMError(RuntimeError):
    """The model could not produce a usable answer (caller sends the fallback and escalates)."""


class LLMUnavailable(LLMError):
    """Provider unreachable / overloaded / rate-limited after SDK retries."""


class LLMRefusal(LLMError):
    """The model declined (stop_reason == "refusal")."""


@dataclass
class LLMRequest:
    system_stable: str  # cached prefix (identical for all tenants)
    system_dynamic: str  # per-turn context
    messages: list[dict[str, Any]]  # [{"role": "user"|"assistant", "content": str}], ends with the user turn
    tools: list[dict[str, Any]]
    language: str
    # Structured view of the same context (used by MockLLM; ignored by real models).
    latest_text: str = ""
    state: dict[str, Any] = field(
        default_factory=dict
    )  # mutable dialogue slots, persisted by the orchestrator
    today: date | None = None
    now_local: datetime | None = None
    tenant_name: str = ""
    contact_name: str = ""
    branches: list[dict[str, Any]] = field(default_factory=list)
    articles: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    iterations: int = 1
    request_ids: list[str] = field(default_factory=list)


class LLM(Protocol):
    provider: str
    model: str

    def run(self, req: LLMRequest, execute: ToolExecutor) -> LLMResult: ...


# ---------------------------------------------------------------- Anthropic
class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        effort: str = "low",
        max_tokens: int = 8000,
        max_iterations: int = 8,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        client: Any = None,
    ):
        import anthropic

        self._anthropic = anthropic
        self.model = model or get_settings().anthropic_model
        self.effort = effort  # "low" | "medium": chat latency matters more than deep reasoning
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self.client = client or anthropic.Anthropic(
            api_key=api_key or get_settings().anthropic_api_key,
            max_retries=max_retries,  # SDK retries connection errors, 408/409/429/5xx with backoff
            timeout=anthropic.Timeout(timeout_s, connect=5.0),
        )

    def _create(
        self, system: list[dict[str, Any]], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        a = self._anthropic
        try:
            return self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto"},
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except (a.APIConnectionError, a.RateLimitError, a.InternalServerError) as exc:
            raise LLMUnavailable(f"{exc.__class__.__name__}: {exc}") from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailable(f"HTTP {exc.status_code}: {exc.message}") from exc
            raise LLMError(f"HTTP {exc.status_code}: {exc.message}") from exc

    def run(self, req: LLMRequest, execute: ToolExecutor) -> LLMResult:
        system = [
            {"type": "text", "text": req.system_stable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": req.system_dynamic},
        ]
        messages: list[dict[str, Any]] = list(req.messages)
        result = LLMResult(text="", iterations=0)
        for _ in range(self.max_iterations):
            resp = self._create(system, messages, req.tools)
            result.iterations += 1
            usage = getattr(resp, "usage", None)
            if usage is not None:
                result.input_tokens += (
                    (usage.input_tokens or 0)
                    + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                    + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                )
                result.output_tokens += usage.output_tokens or 0
            if rid := getattr(resp, "_request_id", None):
                result.request_ids.append(str(rid))
            result.stop_reason = resp.stop_reason or ""

            if resp.stop_reason == "refusal":
                raise LLMRefusal("model refused to answer")
            if resp.stop_reason == "max_tokens":
                # A truncated turn may contain a cut-off tool_use: never execute it.
                raise LLMError("response truncated (max_tokens)")
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "tool_use" and tool_uses:
                # Append the full content unchanged (thinking blocks must be passed back as-is).
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in tool_uses:
                    out = execute(block.name, block.input)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(out, ensure_ascii=False, default=str),
                            "is_error": not out.get("ok", False),
                        }
                    )
                messages.append({"role": "user", "content": results})
                continue
            result.text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return result
        raise LLMError(f"tool loop exceeded {self.max_iterations} iterations")


# ---------------------------------------------------------------- factory
_instance: LLM | None = None


def set_llm(llm: LLM | None) -> None:
    """Install an adapter instance (tests / wiring). None restores settings-based selection."""
    global _instance
    _instance = llm


def get_llm() -> LLM:
    global _instance
    if _instance is None:
        s = get_settings()
        if s.llm_provider == "anthropic":
            if not s.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY is not configured")
            _instance = AnthropicLLM()
        else:
            from app.ai.mock_llm import MockLLM

            _instance = MockLLM()
    return _instance


def __getattr__(name: str) -> Any:  # re-export MockLLM lazily (avoids an import cycle)
    if name == "MockLLM":
        from app.ai.mock_llm import MockLLM

        return MockLLM
    raise AttributeError(name)
