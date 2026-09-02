from __future__ import annotations

import unittest
from unittest.mock import patch

from app.llm.client import OpenAICompatibleClient


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": "完成"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        }


class FakeAsyncClient:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return FakeResponse()


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_successful_requests_and_token_usage_without_exposing_the_key(self):
        client = OpenAICompatibleClient(api_key="secret-for-test", base_url="https://example.test", model="demo")
        with patch("app.llm.client.httpx.AsyncClient", FakeAsyncClient):
            message = await client.complete([{"role": "user", "content": "test"}], [])
            await client.complete([{"role": "user", "content": "again"}], [])

        self.assertEqual(message["content"], "完成")
        self.assertEqual(client.request_count, 2)
        self.assertEqual(client.usage_totals, {"prompt_tokens": 22, "completion_tokens": 6, "total_tokens": 28})
        self.assertNotIn("secret-for-test", repr(client.usage_totals))
