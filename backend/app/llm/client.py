from __future__ import annotations

from typing import Any
import httpx


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """仅封装厂商 HTTP API；循环、工具执行和上下文均留在本项目。"""

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.api_key:
            raise LLMError("llm_api_key_missing")
        payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.2}
        try:
            async with httpx.AsyncClient(timeout=75) as client:
                response = await client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
            raise LLMError("llm_request_failed") from error
