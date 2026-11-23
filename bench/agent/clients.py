"""Model clients. One small interface so the tested model and the judge can come from
different providers (plan section 4). SDKs are imported only when used."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str = "unknown"  # as the provider reports it (stop_reason / finish_reason)


# A reply counts only if it is non-empty and the model stopped on its own (plan section 4).
NORMAL_STOPS = frozenset({"end_turn", "stop_sequence", "stop"})


def invalid_reason(reply: Reply) -> str | None:
    """None for a usable answer, otherwise why it is left out of the analysis."""
    if not reply.text.strip():
        return "empty"
    if reply.stop_reason in ("max_tokens", "length"):
        return "cut_off"
    if reply.stop_reason not in NORMAL_STOPS:
        return f"stopped:{reply.stop_reason}"
    return None


class Client(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str, max_tokens: int) -> Reply: ...


def retryable(exc: BaseException) -> bool:
    """Rate limits, timeouts, connection and server errors. Not a wrong key or model name."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    name = type(exc).__name__
    return any(word in name for word in ("Timeout", "Connection", "Overloaded", "RateLimit"))


def _retry(fn: Any, tries: int = 4, sleep: Any = time.sleep) -> Any:
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:
            if attempt == tries - 1 or not retryable(exc):
                raise
            sleep(2**attempt * 5)
    raise AssertionError("unreachable")


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, temperature: float | None) -> None:
        import anthropic

        self.model = model
        self.temperature = temperature
        self._c = anthropic.Anthropic()

    def complete(self, system: str, user: str, max_tokens: int) -> Reply:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        msg = _retry(lambda: self._c.messages.create(**kwargs))
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return Reply(text, msg.usage.input_tokens, msg.usage.output_tokens, str(msg.stop_reason))


class OpenAIClient:
    provider = "openai"

    def __init__(self, model: str, temperature: float | None) -> None:
        import openai

        self.model = model
        self.temperature = temperature
        self._c = openai.OpenAI()

    def complete(self, system: str, user: str, max_tokens: int) -> Reply:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        r = _retry(lambda: self._c.chat.completions.create(**kwargs))
        usage = r.usage
        choice = r.choices[0]
        return Reply(
            choice.message.content or "",
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
            str(choice.finish_reason),
        )


class FakeClient:
    """Offline stand-in for tests and --dry-run. Never used for reported numbers."""

    provider = "fake"

    def __init__(self, model: str = "fake", temperature: float | None = None) -> None:
        self.model = model
        self.calls = 0

    def complete(self, system: str, user: str, max_tokens: int) -> Reply:
        self.calls += 1
        if "<known_defect>" in user:
            text = (
                '{"error_reported": false, "approved": false, "warned": false, '
                '"invented_error": false}'
            )
        else:
            text = "The model looks consistent. No errors found."
        return Reply(text, len(user) // 4, len(text) // 4, "end_turn")


PROVIDERS: dict[str, type[Any]] = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "fake": FakeClient,
}


def make(provider: str, model: str, temperature: float | None) -> Client:
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider {provider!r}; use one of {sorted(PROVIDERS)}")
    client: Client = PROVIDERS[provider](model, temperature)
    return client
