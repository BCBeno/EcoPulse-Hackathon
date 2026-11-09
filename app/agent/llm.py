"""OpenAI-compatible chat completions: streaming, tool calls, provider fallback (Groq, then OpenRouter)."""

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import openai
from openai import AsyncOpenAI

from app.config import Settings

log = logging.getLogger("ecopulse.llm")

MAX_TOKENS = 500
# Any API failure (rate limit, outage, rejected tool arguments mid-stream...) moves on to the next
# provider, as long as nothing has been streamed to the user yet.
FALLBACK_ERRORS = (openai.APIError,)
RATE_LIMIT_PAUSE_S = 20.0


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def parsed(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Completion:
    provider: str
    model: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


class LlmUnavailable(Exception):
    """No provider could answer; the caller falls back to the rules engine."""


def providers(settings: Settings) -> list[Provider]:
    chain = []
    if settings.groq_api_key:
        for model in (m.strip() for m in settings.groq_model.split(",")):
            if model:
                chain.append(
                    Provider(f"groq:{model}", "https://api.groq.com/openai/v1", settings.groq_api_key, model)
                )
    if settings.openrouter_api_key and settings.openrouter_model:
        chain.append(
            Provider(
                "openrouter",
                "https://openrouter.ai/api/v1",
                settings.openrouter_api_key,
                settings.openrouter_model,
                {"HTTP-Referer": "https://ecopulse.bcbeno.me", "X-Title": "EcoPulse"},
            )
        )
    return chain


class LlmClient:
    """Streams one completion, trying providers in order until one starts answering."""

    def __init__(self, settings: Settings, chain: list[Provider] | None = None):
        self.chain = providers(settings) if chain is None else chain
        self.paused_until: dict[str, float] = {}
        self.clients = {
            p.name: AsyncOpenAI(
                api_key=p.api_key,
                base_url=p.base_url,
                timeout=settings.llm_timeout_s,
                max_retries=0,
                default_headers=p.headers or None,
            )
            for p in self.chain
        }

    @property
    def available(self) -> bool:
        return bool(self.chain)

    async def stream(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[str | Completion]:
        """Yields text deltas (str) and finally one Completion."""
        for provider in self.chain:
            if self.paused_until.get(provider.name, 0) > time.monotonic():
                continue
            started = False
            try:
                async for item in self._stream_one(provider, messages, tools):
                    if isinstance(item, str):
                        started = True
                    yield item
                return
            except FALLBACK_ERRORS as error:
                if isinstance(error, openai.RateLimitError):
                    # Skip this provider for a while instead of paying a failed request every time.
                    self.paused_until[provider.name] = time.monotonic() + RATE_LIMIT_PAUSE_S
                if started:
                    raise LlmUnavailable(f"{provider.name}: interrupted") from error
                log.warning("LLM provider %s failed: %s", provider.name, type(error).__name__)
        raise LlmUnavailable("no provider answered")

    async def _stream_one(
        self, provider: Provider, messages: list[dict], tools: list[dict] | None
    ) -> AsyncIterator[str | Completion]:
        request: dict = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.3,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        stream = await self.clients[provider.name].chat.completions.create(**request)
        result = Completion(provider=provider.name, model=provider.model)
        calls: dict[int, ToolCall] = {}
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                result.tokens_in = chunk.usage.prompt_tokens or 0
                result.tokens_out = chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                result.text += delta.content
                yield delta.content
            for part in delta.tool_calls or []:
                call = calls.setdefault(part.index, ToolCall(id="", name="", arguments=""))
                if part.id:
                    call.id = part.id
                if part.function and part.function.name:
                    call.name += part.function.name
                if part.function and part.function.arguments:
                    call.arguments += part.function.arguments
        result.tool_calls = [c for _, c in sorted(calls.items()) if c.name]
        if not result.tokens_in:
            # Some providers omit usage in streams; estimate (~4 characters per token).
            result.tokens_in = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
            result.tokens_out = (len(result.text) + sum(len(c.arguments) for c in result.tool_calls)) // 4
        yield result
