from __future__ import annotations

import unittest
from unittest.mock import patch

from app.llm.client import AcceptanceCriterion, OpenAICompatibleClient, TaskAnalysis, TaskReview


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


class AnalysisResponse(FakeResponse):
    def json(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": '{"continuation":false,"adaptive_mode":"multi","workspace_preferences":[],"criteria":[{"id":"AC1","description":"实际执行启动命令","kind":"command","verification_hint":"检查真实退出码"}]}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }


class AnalysisAsyncClient(FakeAsyncClient):
    async def post(self, *_args, **_kwargs):
        return AnalysisResponse()


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

    async def test_task_semantics_are_returned_by_one_structured_api_call(self):
        client = OpenAICompatibleClient(api_key="secret-for-test", base_url="https://example.test", model="demo")
        with patch("app.llm.client.httpx.AsyncClient", AnalysisAsyncClient):
            analysis = await client.analyze_task(
                task='function buildQuery() { return "created_at"; } 还是没有执行',
                conversation_context="上一轮用户要求检查页面",
                requested_mode="adaptive",
            )

        self.assertEqual(analysis, TaskAnalysis(
            requires_command=True,
            adaptive_mode="multi",
            criteria=(AcceptanceCriterion("AC1", "实际执行启动命令", "command", "检查真实退出码"),),
        ))
        self.assertEqual(client.request_count, 1)

    def test_task_analysis_rejects_loose_or_missing_fields(self):
        with self.assertRaises(ValueError):
            TaskAnalysis.from_payload({"requires_command": True})
        with self.assertRaises(ValueError):
            TaskAnalysis.from_payload({"continuation": 1, "requires_file_change": False, "requires_command": False, "adaptive_mode": "single", "workspace_preferences": []})

    def test_task_contract_rejects_duplicate_or_unknown_criteria(self):
        base = {"continuation": False, "adaptive_mode": "single", "workspace_preferences": []}
        with self.assertRaises(ValueError):
            TaskAnalysis.from_payload({**base, "criteria": [{"id": "AC1", "description": "x", "kind": "guess", "verification_hint": ""}]})
        with self.assertRaises(ValueError):
            TaskAnalysis.from_payload({**base, "criteria": [
                {"id": "AC1", "description": "x", "kind": "response", "verification_hint": ""},
                {"id": "AC1", "description": "y", "kind": "response", "verification_hint": ""},
            ]})

    def test_structured_review_must_cover_exact_contract(self):
        allowed = {"AC1", "AC2"}
        with self.assertRaises(ValueError):
            TaskReview.from_payload({"assessments": [{"criterion_id": "AC1", "status": "satisfied"}]}, allowed)
