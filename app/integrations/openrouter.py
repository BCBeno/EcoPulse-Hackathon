"""OpenRouter Chat Completions transport for the same validated tool-selection protocol."""

import httpx

from app.integrations.http import ProviderError
from app.integrations.llm import OpenAIResponsesProvider


class OpenRouterProvider(OpenAIResponsesProvider):
    async def request(self, payload):
        messages = [{"role": "system", "content": payload["instructions"]}]
        for item in payload["input"]:
            if item.get("type") == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item["call_id"],
                                "type": "function",
                                "function": {
                                    "name": item["name"],
                                    "arguments": item["arguments"],
                                },
                            }
                        ],
                    }
                )
            elif item.get("type") == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
            else:
                messages.append(item)
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": 1200,
            "provider": {"require_parameters": True},
        }
        if "tools" in payload:
            self.tool_definitions = [
                {"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}}
                for tool in payload["tools"]
            ]
            body.update(
                tools=self.tool_definitions,
                tool_choice=payload["tool_choice"],
            )
        if "text" in payload:
            body.update(tools=self.tool_definitions, tool_choice="none")
            schema = payload["text"]["format"]
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema["name"],
                    "strict": True,
                    "schema": schema["schema"],
                },
            }
        try:
            response = await self.http.client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}", "X-Title": "EcoPulse"},
                json=body,
                timeout=20,
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ValueError("Incomplete response")
            message = choice["message"]
            output = [
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
                for call in message.get("tool_calls", [])
            ]
            if not output and message.get("content"):
                output.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": message["content"]},
                        ],
                    }
                )
            return {"status": "completed", "output": output}
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("llm_unavailable") from None
