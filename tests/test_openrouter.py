import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.http import ProviderError, ProviderHTTP
from app.integrations.openrouter import OpenRouterProvider


async def test_openrouter_tools_and_structured_output():
    settings = Settings(
        _env_file=None, llm_provider="openrouter", llm_api_key="fixture", llm_model="fixture-model"
    )
    calls = []

    def handle(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "fixture-model"
        assert body["tools"][0]["function"]["name"] == "get_sources"
        assert body["provider"]["require_parameters"] is True
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-fixture",
                        "type": "function",
                        "function": {"name": "get_sources", "arguments": "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assert body["messages"][-1]["role"] == "tool"
            assert body["messages"][-1]["tool_call_id"] == "call-fixture"
            assert body["response_format"]["json_schema"]["strict"] is True
            assert body["tool_choice"] == "none"
            message = {
                "role": "assistant",
                "content": json.dumps({"focus": "sources", "evidence_ids": ["fixture-evidence"]}),
            }
            finish = "stop"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})

    async def tool():
        return {"evidence": [{"id": "fixture-evidence"}]}

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    try:
        result, tools = await OpenRouterProvider(settings, http).select(
            "Surse?", {"get_sources": ("Read sources", tool)}
        )
        assert result.focus == "sources" and result.evidence_ids == ["fixture-evidence"]
        assert "get_sources" in tools and len(calls) == 2
    finally:
        await http.close()


async def test_openrouter_rejects_unknown_tool_and_incomplete_response():
    settings = Settings(_env_file=None, llm_api_key="fixture", llm_model="fixture-model")
    http = ProviderHTTP(
        settings,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"choices": [{"finish_reason": "length", "message": {"content": "truncated"}}]}
            )
        ),
    )
    try:
        with pytest.raises(ProviderError, match="llm_unavailable"):
            await OpenRouterProvider(settings, http).select("Surse?", {})
    finally:
        await http.close()


def test_provider_url_alias_and_invalid_provider():
    assert (
        Settings(_env_file=None, llm_provider="https://openrouter.ai/api/v1/chat/completions").llm_provider
        == "openrouter"
    )
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Settings(_env_file=None, llm_provider="https://untrusted.invalid")
