import asyncio
import json
import sys
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from app.agents.orchestrator import Orchestrator, continuation_lineage_has_successful_write, is_file_mutation_command, is_inspection_command, normalize_agent_config, parse_reviewer_basis, parse_reviewer_verdict, resolve_agent_mode, turn_has_successful_write
from app.core.settings import settings
from app.core.models import AgentEvent, AgentRole, EventType
from app.core.auth import LocalAuth
from app.core.settings import Settings
from app.core.checkpoints import CheckpointManager, content_sha256
from app.core.extensions import HookConfig, load_project_rules
from app.storage.sqlite_store import SQLiteStore
from app.runtime.workspace import Workspace
from app.tools.registry import ToolRegistry
from app.llm.client import AcceptanceCriterion, CriterionReview, TaskAnalysis, TaskReview


CHANGE_ANALYSIS = TaskAnalysis(requires_file_change=True)
CONTINUATION_CHANGE_ANALYSIS = TaskAnalysis(continuation=True, requires_file_change=True)
COMMAND_ANALYSIS = TaskAnalysis(requires_command=True)


class FakeClient:
    async def complete(self, messages, tools):
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: PASS 已检查完成"}
        return {"role": "assistant", "content": "已完成"}


class RepeatingToolClient:
    async def complete(self, messages, tools):
        if not tools and "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: PASS 已停止重复调用"}
        if messages[-1].get("role") == "tool" and "repeated_tool_call" in messages[-1].get("content", ""):
            verdict = "VERDICT: PASS " if "reviewer agent" in messages[0]["content"] else ""
            return {"role": "assistant", "content": f"{verdict}已停止重复调用"}
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "loop", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}


class CapturingClient:
    def __init__(self):
        self.system_messages = []

    async def complete(self, messages, tools):
        self.system_messages.append(messages[0]["content"])
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: PASS 已检查完成"}
        return {"role": "assistant", "content": "已完成"}


class WritingClient:
    def __init__(self):
        self.written = False

    async def complete(self, messages, tools):
        names = {tool["function"]["name"] for tool in tools}
        if "write_file" in names and not self.written:
            self.written = True
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"created.txt","content":"created by MossCode\\n"}'}}]}
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: PASS 写入结果已验证"}
        return {"role": "assistant", "content": "已完成"}


class NeverWritesClient:
    async def complete(self, messages, tools):
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: PASS 只读检查完成"}
        return {"role": "assistant", "content": "给出计划，但没有写入。"}


class RejectingReviewerClient(FakeClient):
    async def complete(self, messages, tools):
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: NEEDS_WORK\nBASIS: FAILED_VERIFICATION\nEVIDENCE: unittest exit_code=1\nACTION: 修复失败测试"}
        return await super().complete(messages, tools)


class ReviewerFinalizationClient(FakeClient):
    async def complete(self, messages, tools):
        if "reviewer agent" not in messages[0]["content"]:
            return await super().complete(messages, tools)
        if not tools:
            return {"role": "assistant", "content": "VERDICT: PASS 已根据现有成功证据完成审查。"}
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "inspect", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}


class StructuredEvidenceClient(FakeClient):
    def __init__(self):
        self.written = False
        self.verified = False

    async def complete(self, messages, tools):
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "已检查工具证据，未输出规范标记。"}
        names = {tool["function"]["name"] for tool in tools}
        if "write_file" in names and not self.written:
            self.written = True
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"evidence.txt","content":"ok\\n"}'}}]}
        if "run_command" in names and not self.verified:
            self.verified = True
            command = f'"{sys.executable}" -c "print(123)"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "verify", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        return {"role": "assistant", "content": "已完成"}


class ScopeDriftingReviewerClient(StructuredEvidenceClient):
    """模拟 Reviewer 在测试成功后擅自增加用户从未要求的入口规范。"""
    async def complete(self, messages, tools):
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: NEEDS_WORK 还应增加 if __name__ == '__main__' 入口。"}
        return await super().complete(messages, tools)


class MixedVerificationClient(FakeClient):
    def __init__(self):
        self.stage = 0

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "reviewer agent" in system:
            return {"role": "assistant", "content": "VERDICT: PASS 针对性验证已通过，后续失败与本次改动无关。"}
        if "coder agent" not in system:
            return {"role": "assistant", "content": "已完成。"}
        if self.stage == 0:
            self.stage += 1
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"fixed.txt","content":"fixed\\n"}'}}]}
        if self.stage == 1:
            self.stage += 1
            command = f'"{sys.executable}" -c "print(\'target verified\')"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "target-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        if self.stage == 2:
            self.stage += 1
            command = f'"{sys.executable}" -c "raise SystemExit(1)"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "unrelated-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        return {"role": "assistant", "content": "目标修复已通过针对性验证，另有无关环境测试失败。"}


class ConversationalMemoryClient:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return {"role": "assistant", "content": "我是小m，你的统一 MossCode 助手。"}


class SerializedToolRecoveryClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        names = {tool["function"]["name"] for tool in tools}
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="exec_command">echo promised</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>',
            }
        if "run_command" in names and not any(message.get("role") == "tool" for message in messages):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "actual-command",
                    "type": "function",
                    "function": {"name": "exec_command", "arguments": '{"cmd":"echo started"}'},
                }],
            }
        return {"role": "assistant", "content": "命令已根据真实输出执行完成。"}


class RepairingWorkflowClient:
    def __init__(self):
        self.initial_written = False
        self.initial_verified = False
        self.repair_written = False
        self.repair_verified = False
        self.reviewer_calls = 0
        self.reviewer_inputs = []

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "reviewer agent" in system:
            self.reviewer_calls += 1
            self.reviewer_inputs.append(messages[1]["content"])
            if self.reviewer_calls == 1:
                return {"role": "assistant", "content": "VERDICT: NEEDS_WORK\nBASIS: USER_REQUIREMENT\nEVIDENCE: result.txt 内容为 wrong，用户要求 correct\nACTION: 将 result.txt 改为 correct"}
            return {"role": "assistant", "content": "VERDICT: PASS 已核对修复内容和成功验证。"}
        if "coder agent" not in system:
            return {"role": "assistant", "content": "已完成本角色工作。"}

        repairing = "有限返工" in messages[1]["content"]
        if repairing and not self.repair_written:
            self.repair_written = True
            arguments = json.dumps({"path": "result.txt", "content": "correct\n", "expected_sha256": content_sha256("wrong\n")})
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-write", "type": "function", "function": {"name": "write_file", "arguments": arguments}}]}
        if repairing and not self.repair_verified:
            self.repair_verified = True
            command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').read_text().strip() == \'correct\'"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command, "criterion_ids": ["AC1"], "evidence_kind": "verification"})}}]}
        if not repairing and not self.initial_written:
            self.initial_written = True
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "initial-write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"result.txt","content":"wrong\\n"}'}}]}
        if not repairing and not self.initial_verified:
            self.initial_verified = True
            command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').exists()"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "initial-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        return {"role": "assistant", "content": "实现和验证已完成。"}


class ResumeExistingClient:
    def __init__(self):
        self.verified = False
        self.reviewer_context = ""

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "reviewer agent" in system:
            self.reviewer_context = messages[1]["content"]
            return {"role": "assistant", "content": "VERDICT: PASS 已确认保留的实现和最新验证。"}
        names = {tool["function"]["name"] for tool in tools}
        if "coder agent" in system and "run_command" in names and not self.verified:
            self.verified = True
            command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'existing.txt\').read_text().strip() == \'ready\'"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "resume-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        return {"role": "assistant", "content": "现有实现已经符合要求。"}


class TurnLimitedResumeClient:
    def __init__(self):
        self.command_index = 0

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "reviewer agent" in system:
            return {"role": "assistant", "content": "VERDICT: PASS 已依据成功验证复核。"}
        names = {tool["function"]["name"] for tool in tools}
        if "coder agent" in system and "run_command" in names:
            self.command_index += 1
            command = f'"{sys.executable}" -c "print({self.command_index})"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"verify-{self.command_index}", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        return {"role": "assistant", "content": "已完成本角色工作。"}


class InspectionHeavyRepairClient:
    def __init__(self):
        self.initial_written = False
        self.initial_verified = False
        self.repair_reads = 0
        self.repair_written = False
        self.repair_verified = False
        self.reviewer_calls = 0
        self.repair_tool_names = []

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        names = {tool["function"]["name"] for tool in tools}
        if "reviewer agent" in system:
            self.reviewer_calls += 1
            verdict = "VERDICT: NEEDS_WORK\nBASIS: USER_REQUIREMENT\nEVIDENCE: result.txt 尚不是 fixed\nACTION: 将 result.txt 修正为 fixed" if self.reviewer_calls == 1 else "VERDICT: PASS 修复和验证均已确认。"
            return {"role": "assistant", "content": verdict}
        if "coder agent" not in system:
            return {"role": "assistant", "content": "已完成。"}
        repairing = "有限返工" in messages[1]["content"]
        if repairing:
            self.repair_tool_names.append(sorted(names))
        if not repairing and not self.initial_written:
            self.initial_written = True
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "initial-write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"result.txt","content":"wrong\\n"}'}}]}
        if not repairing and not self.initial_verified:
            self.initial_verified = True
            command = f'"{sys.executable}" -c "print(1)"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "initial-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        if repairing and not self.repair_written and "run_command" in names and self.repair_reads < 10:
            self.repair_reads += 1
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"repair-read-{self.repair_reads}", "type": "function", "function": {"name": "run_command", "arguments": '{"command":"type result.txt"}'}}]}
        if repairing and not self.repair_written:
            self.repair_written = True
            arguments = json.dumps({"path": "result.txt", "content": "fixed\n", "expected_sha256": content_sha256("wrong\n")})
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-write", "type": "function", "function": {"name": "write_file", "arguments": arguments}}]}
        if repairing and not self.repair_verified:
            self.repair_verified = True
            command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').read_text().strip() == \'fixed\'"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command, "criterion_ids": ["AC1"], "evidence_kind": "verification"})}}]}
        return {"role": "assistant", "content": "实现已完成。"}


class CriterionLedgerClient:
    def __init__(self):
        self.stage = 0
        self.review_evidence = {}

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "coder agent" not in system:
            return {"role": "assistant", "content": "已完成本角色工作。"}
        calls = [
            ("write_file", {"path": "first.txt", "content": "first\n", "criterion_ids": ["AC1"]}),
            ("run_command", {"command": f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'first.txt\').exists()"', "criterion_ids": ["AC1"], "evidence_kind": "verification"}),
            ("write_file", {"path": "second.txt", "content": "second\n", "criterion_ids": ["AC2"]}),
            ("run_command", {"command": f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'second.txt\').exists()"', "criterion_ids": ["AC2"], "evidence_kind": "verification"}),
            # A global diagnostic may fail, but it is deliberately not evidence for either criterion.
            ("run_command", {"command": f'"{sys.executable}" -c "raise SystemExit(1)"', "criterion_ids": [], "evidence_kind": "verification"}),
        ]
        if self.stage < len(calls):
            name, arguments = calls[self.stage]
            self.stage += 1
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"ledger-{self.stage}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        return {"role": "assistant", "content": "两个独立验收项均已实现并完成针对性验证。"}

    async def review_task(self, *, analysis, evidence):
        self.review_evidence = evidence
        return TaskReview(tuple(
            CriterionReview(item.id, "satisfied", "linked runtime evidence")
            for item in analysis.criteria
        ), "逐项证据已核对。")


class StructuredRepairClient:
    def __init__(self):
        self.initial_stage = 0
        self.repair_stage = 0
        self.review_calls = 0

    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "coder agent" not in system:
            return {"role": "assistant", "content": "已完成本角色工作。"}
        repairing = "唯一一次内部返工" in messages[1]["content"]
        if not repairing:
            calls = [
                ("write_file", {"path": "result.txt", "content": "wrong\n", "criterion_ids": ["AC1"]}),
                ("run_command", {"command": f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').exists()"', "criterion_ids": ["AC1"], "evidence_kind": "verification"}),
            ]
            if self.initial_stage < len(calls):
                name, arguments = calls[self.initial_stage]
                self.initial_stage += 1
                return {"role": "assistant", "content": None, "tool_calls": [{"id": f"initial-{self.initial_stage}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            return {"role": "assistant", "content": "初始实现已完成。"}
        calls = [
            ("write_file", {"path": "result.txt", "content": "correct\n", "expected_sha256": content_sha256("wrong\n"), "criterion_ids": ["AC1"]}),
            ("run_command", {"command": f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').read_text().strip() == \'correct\'"', "criterion_ids": ["AC1"], "evidence_kind": "verification"}),
        ]
        if self.repair_stage < len(calls):
            name, arguments = calls[self.repair_stage]
            self.repair_stage += 1
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"repair-{self.repair_stage}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        return {"role": "assistant", "content": "定向修复已完成。"}

    async def review_task(self, *, analysis, evidence):
        self.review_calls += 1
        return TaskReview((CriterionReview("AC1", "unmet", "result.txt is wrong", "写成 correct 并验证内容"),), "发现一个明确缺口。")


class SingleInspectionBudgetClient:
    def __init__(self):
        self.inspection_attempts = 0
        self.executed = False
        self.restricted_kinds = []

    async def complete(self, messages, tools):
        names = {tool["function"]["name"] for tool in tools}
        if "list_files" in names:
            self.inspection_attempts += 1
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"inspect-{self.inspection_attempts}", "type": "function", "function": {"name": "list_files", "arguments": '{"path":"."}'}}]}
        if "run_command" in names and not self.executed:
            command_schema = next(tool for tool in tools if tool["function"]["name"] == "run_command")
            self.restricted_kinds = command_schema["function"]["parameters"]["properties"]["evidence_kind"]["enum"]
            self.executed = True
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "verify", "type": "function", "function": {"name": "run_command", "arguments": '{"command":"echo verified","criterion_ids":["AC1"],"evidence_kind":"action"}'}}]}
        return {"role": "assistant", "content": "真实命令已经完成。"}


class ContinuationReplayClient:
    def __init__(self):
        self.inspections = 0
        self.verified = False

    async def complete(self, messages, tools):
        names = {tool["function"]["name"] for tool in tools}
        if "list_files" in names:
            self.inspections += 1
            return {"role": "assistant", "content": None, "tool_calls": [{"id": f"read-{self.inspections}", "type": "function", "function": {"name": "list_files", "arguments": '{"path":".","criterion_ids":["AC2"]}'}}]}
        if not self.verified:
            self.verified = True
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "targeted-verification", "type": "function", "function": {"name": "run_command", "arguments": '{"command":"echo sort_by verified","criterion_ids":["AC1","AC3"],"evidence_kind":"verification"}'}}]}
        return {"role": "assistant", "content": "保留的修复已完成针对性验证。"}


class InternalReviewSummaryClient:
    async def complete(self, messages, tools):
        system = messages[0]["content"]
        if "explorer agent" in system:
            return {"role": "assistant", "content": "当前目录包含 `main.py`、`static/` 和 `tests/`，是一个 FastAPI 任务管理项目。"}
        return {"role": "assistant", "content": "已整理请求。"}

    async def review_task(self, *, analysis, evidence):
        return TaskReview(tuple(
            CriterionReview(item.id, "satisfied", "internal evidence") for item in analysis.criteria
        ), "Both acceptance criteria are satisfied. AC1 and AC2 are complete.")


class CoreTests(unittest.TestCase):
    def test_continuation_can_reuse_a_successful_write_from_its_reference_turn(self):
        events = [
            AgentEvent(
                type=EventType.TOOL_FINISHED,
                session_id="session",
                turn_id="turn-19",
                role=AgentRole.CODER,
                summary="written",
                payload={"tool": "replace_text", "result": {"ok": True, "code": "ok"}},
            ),
            AgentEvent(
                type=EventType.TASK_FAILED,
                session_id="session",
                turn_id="turn-19",
                summary="later verification failed",
            ),
        ]
        self.assertTrue(turn_has_successful_write(events, "turn-19"))
        self.assertFalse(turn_has_successful_write(events, "turn-20"))

    def test_continuation_lineage_skips_failed_continue_turns_without_losing_checkpoint(self):
        events = [
            AgentEvent(
                type=EventType.TOOL_FINISHED, session_id="session", turn_id="turn-19", role=AgentRole.CODER,
                summary="written", payload={"tool": "replace_text", "result": {"ok": True}},
            ),
            AgentEvent(
                type=EventType.AGENT_STARTED, session_id="session", turn_id="turn-20", role=AgentRole.SINGLE,
                summary="continue", payload={"task_analysis": {"continuation": True}},
            ),
            AgentEvent(
                type=EventType.TASK_FAILED, session_id="session", turn_id="turn-20", summary="no new write",
            ),
        ]
        self.assertTrue(continuation_lineage_has_successful_write(events, ["turn-20", "turn-19"]))
        stopped = [events[0], events[1].model_copy(update={"payload": {"task_analysis": {"continuation": False}}})]
        self.assertFalse(continuation_lineage_has_successful_write(stopped, ["turn-20", "turn-19"]))

    def test_single_agent_inspection_budget_forces_execution_before_turn_limit(self):
        analysis = TaskAnalysis(criteria=(AcceptanceCriterion("AC1", "执行验证命令", "command"),))

        async def exercise(directory):
            events = []
            client = SingleInspectionBudgetClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "single-budget", "继续验证", ToolRegistry(Workspace(directory)), client,
                settings, publish, execution_mode="single", task_analysis=analysis,
            )
            return await runner.run(), runner, client, events

        with TemporaryDirectory() as directory:
            completed, runner, client, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(client.inspection_attempts, 5)
            self.assertTrue(client.executed)
            self.assertEqual(client.restricted_kinds, ["verification", "action"])
            self.assertEqual(runner._unmet_criteria(), [])
            self.assertFalse(any(event.type == EventType.TASK_FAILED for event in events))

    def test_one_command_can_close_file_verification_and_command_criteria(self):
        analysis = TaskAnalysis(criteria=(
            AcceptanceCriterion("AC1", "验证保留的文件修改", "file_change"),
            AcceptanceCriterion("AC2", "实际运行验证命令", "command"),
        ))

        async def exercise(directory):
            runner = Orchestrator(
                "mixed-command", "继续修复", ToolRegistry(Workspace(directory)), FakeClient(),
                settings, lambda event: asyncio.sleep(0), task_analysis=analysis, resume_existing=True,
            )
            result = await runner._execute(AgentRole.SINGLE, "run_command", {
                "command": "echo verified",
                "criterion_ids": ["AC1", "AC2"],
                "evidence_kind": "verification",
            })
            return runner, result

        with TemporaryDirectory() as directory:
            runner, result = asyncio.run(exercise(directory))
            self.assertTrue(result.ok)
            self.assertEqual(runner._unmet_criteria(), [])

    def test_turn_21_replay_resumes_checkpoint_then_transitions_from_discovery_to_verification(self):
        analysis = TaskAnalysis(continuation=True, adaptive_mode="single", criteria=(
            AcceptanceCriterion("AC1", "验证上一轮保留的前端修复", "file_change"),
            AcceptanceCriterion("AC2", "只读核对非法排序值已经消失", "inspection"),
            AcceptanceCriterion("AC3", "实际运行可重复验证命令", "command"),
        ))

        async def exercise(directory):
            events = []
            client = ContinuationReplayClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "turn-21", "继续修复", ToolRegistry(Workspace(directory)), client,
                settings, publish, execution_mode="single", resume_existing=True,
                reference_task="修复 sort_by", task_analysis=analysis,
            )
            return await runner.run(), runner, client, events

        with TemporaryDirectory() as directory:
            completed, runner, client, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(client.inspections, 5)
            self.assertTrue(client.verified)
            self.assertEqual(runner._unmet_criteria(), [])
            self.assertEqual(events[-1].type, EventType.TASK_FINISHED)

    def test_internal_reviewer_summary_never_replaces_user_facing_workspace_summary(self):
        analysis = TaskAnalysis(adaptive_mode="multi", criteria=(
            AcceptanceCriterion("AC1", "检查工作目录", "inspection"),
            AcceptanceCriterion("AC2", "直接向用户总结", "response"),
        ))

        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "turn-22", "总结当前工作目录", ToolRegistry(Workspace(directory)),
                InternalReviewSummaryClient(), settings, publish, task_analysis=analysis,
            )
            return await runner.run(), events

        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            final = events[-1]
            self.assertEqual(final.type, EventType.TASK_FINISHED)
            self.assertIn("当前目录包含", final.summary)
            self.assertNotIn("acceptance criteria", final.summary)
            self.assertNotIn("AC1", final.summary)
            started = [event.role for event in events if event.type == EventType.AGENT_STARTED]
            self.assertNotIn(AgentRole.CODER, started)

    def test_project_rules_are_sorted_limited_and_hooks_default_off(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("root rule", encoding="utf-8")
            rules_dir = root / ".mosscode" / "rules"
            rules_dir.mkdir(parents=True)
            (rules_dir / "b.md").write_text("second", encoding="utf-8")
            (rules_dir / "a.md").write_text("first", encoding="utf-8")
            rules = load_project_rules(root)
            self.assertEqual([item["path"] for item in rules.sources], ["AGENTS.md", ".mosscode/rules/a.md", ".mosscode/rules/b.md"])
            self.assertLess(rules.text.index("first"), rules.text.index("second"))
            self.assertFalse(HookConfig(root).enabled)

    def test_checkpoint_restore_refuses_a_later_manual_edit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("before\n", encoding="utf-8")
            store = SQLiteStore(str(root / "state.db"))
            session, turn = store.create_session(task="edit", workspace=directory, locale="zh-CN")
            checkpoint_root = root / "checkpoints"
            manager = CheckpointManager(checkpoint_root, store, session.id, turn.id, "test")
            result = ToolRegistry(Workspace(directory), checkpoint=manager).write_file("note.txt", "agent\n", content_sha256("before\n"))
            target.write_text("manual\n", encoding="utf-8")
            ok, code, files = CheckpointManager.restore(checkpoint_root, store, result.meta["checkpoint_id"], root)
            self.assertFalse(ok)
            self.assertEqual(code, "checkpoint_conflict")
            self.assertEqual(files, ["note.txt"])
            self.assertEqual(target.read_text(encoding="utf-8"), "manual\n")

    def test_structured_evidence_completes_when_reviewer_omits_marker(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator("test", "创建并验证文件", ToolRegistry(Workspace(directory)), StructuredEvidenceClient(), settings, publish, task_analysis=CHANGE_ANALYSIS)
            return await runner.run(), events
        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(events[-1].payload["completion_source"], "structured_evidence")

    def test_criterion_ledger_keeps_unrelated_failure_out_of_satisfied_requirements(self):
        analysis = TaskAnalysis(adaptive_mode="multi", criteria=(
            AcceptanceCriterion("AC1", "创建并验证第一个文件", "file_change"),
            AcceptanceCriterion("AC2", "创建并验证第二个文件", "file_change"),
        ))

        async def exercise(directory):
            events = []
            client = CriterionLedgerClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "ledger", "完成两个独立修改", ToolRegistry(Workspace(directory)), client,
                settings, publish, task_analysis=analysis,
            )
            return await runner.run(), runner, client, events

        with TemporaryDirectory() as directory:
            completed, runner, client, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(runner._unmet_criteria(), [])
            self.assertEqual(events[-1].payload["completion_source"], "criterion_review")
            self.assertEqual([item["satisfied"] for item in events[-1].payload["criteria_status"]], [True, True])
            unrelated = client.review_evidence["records"][-1]
            self.assertFalse(unrelated["ok"])
            self.assertEqual(unrelated["criterion_ids"], [])

    def test_structured_reviewer_runs_once_and_repair_requires_new_linked_evidence(self):
        analysis = TaskAnalysis(adaptive_mode="multi", criteria=(
            AcceptanceCriterion("AC1", "result.txt 内容必须为 correct", "file_change"),
        ))

        async def exercise(directory):
            events = []
            client = StructuredRepairClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "repair", "修正 result.txt", ToolRegistry(Workspace(directory)), client,
                settings, publish, task_analysis=analysis,
            )
            return await runner.run(), runner, client, events

        with TemporaryDirectory() as directory:
            completed, runner, client, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(client.review_calls, 1)
            self.assertEqual(runner.repair_cycles, 1)
            self.assertEqual(Path(directory, "result.txt").read_text(encoding="utf-8"), "correct\n")
            self.assertEqual(events[-1].payload["completion_source"], "criterion_evidence_after_repair")

    def test_reviewer_cannot_expand_scope_after_successful_verification(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "创建并验证 evidence.txt", ToolRegistry(Workspace(directory)),
                ScopeDriftingReviewerClient(), settings, publish, task_analysis=CHANGE_ANALYSIS,
            )
            return await runner.run(), runner, events

        with TemporaryDirectory() as directory:
            completed, runner, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(runner.repair_cycles, 0)
            self.assertEqual(events[-1].payload["completion_source"], "structured_evidence")
            self.assertFalse(any(event.type == "task_failed" for event in events))

    def test_reviewer_can_accept_targeted_success_with_a_later_unrelated_failure(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "修复目标文件", ToolRegistry(Workspace(directory)),
                MixedVerificationClient(), settings, publish, task_analysis=CHANGE_ANALYSIS,
            )
            return await runner.run(), runner, events
        with TemporaryDirectory() as directory:
            completed, runner, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(runner.repair_cycles, 0)
            self.assertEqual(events[-1].payload["completion_source"], "reviewer_mixed_evidence")
    def test_local_auth_accepts_configured_credentials_and_unicode_failures(self):
        auth = LocalAuth(Settings(app_username="moss", app_password="安全密码"))
        self.assertIsNone(auth.authenticate("moss", "错误密码"))
        token = auth.authenticate("moss", "安全密码")
        self.assertIsNotNone(token)
        self.assertIn(token, auth.tokens)

    def test_api_task_analysis_controls_write_requirement(self):
        with TemporaryDirectory() as directory:
            runner = Orchestrator("test", "撰写一个花哨的网页", ToolRegistry(Workspace(directory)), FakeClient(), settings, lambda event: asyncio.sleep(0), task_analysis=CHANGE_ANALYSIS)
            self.assertTrue(runner.requires_change)

    def test_reviewer_runs_without_repeating_repository_tools(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator("test", "只读检查", ToolRegistry(Workspace(directory)), ReviewerFinalizationClient(), settings, publish)
            return await runner.run(), events
        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(events[-1].type, "task_finished")
            self.assertFalse(any(event.type == "tool_finished" and event.role == AgentRole.REVIEWER for event in events))

    def test_command_permission_modes_allow_approval_or_deny(self):
        async def exercise(directory):
            approvals = []
            async def approve(role, command, cwd):
                approvals.append((role, command, cwd))
                return True
            runner = Orchestrator("test", "只读检查", ToolRegistry(Workspace(directory)), FakeClient(), settings, lambda event: asyncio.sleep(0), command_mode="ask", request_command_approval=approve)
            allowed = await runner._execute(AgentRole.CODER, "run_command", {"command": f'"{sys.executable}" -c "print(123)"'})
            denied_runner = Orchestrator("test", "只读检查", ToolRegistry(Workspace(directory)), FakeClient(), settings, lambda event: asyncio.sleep(0), command_mode="deny")
            denied = await denied_runner._execute(AgentRole.CODER, "run_command", {"command": f'"{sys.executable}" -c "print(456)"'})
            return allowed, denied, approvals
        with TemporaryDirectory() as directory:
            allowed, denied, approvals = asyncio.run(exercise(directory))
            self.assertTrue(allowed.ok)
            self.assertEqual(denied.code, "command_permission_denied")
            self.assertEqual(len(approvals), 1)

    def test_windows_tail_pipeline_is_rejected_before_requesting_approval(self):
        async def exercise(directory):
            approvals = []
            async def approve(role, command, cwd):
                approvals.append((role, command, cwd))
                return True
            runner = Orchestrator(
                "test", "只读检查", ToolRegistry(Workspace(directory)), FakeClient(), settings,
                lambda event: asyncio.sleep(0), command_mode="ask", request_command_approval=approve,
            )
            result = await runner._execute(AgentRole.CODER, "run_command", {"command": "echo ok | tail -40"})
            return result, approvals
        with TemporaryDirectory() as directory:
            result, approvals = asyncio.run(exercise(directory))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "unsupported_windows_shell_syntax")
            self.assertEqual(approvals, [])

    def test_outside_command_cwd_is_rejected_before_requesting_approval(self):
        async def exercise(directory):
            approvals = []
            async def approve(role, command, cwd):
                approvals.append((role, command, cwd))
                return True
            runner = Orchestrator(
                "test", "只读检查", ToolRegistry(Workspace(directory)), FakeClient(), settings,
                lambda event: asyncio.sleep(0), command_mode="ask", request_command_approval=approve,
            )
            result = await runner._execute(AgentRole.CODER, "run_command", {"command": "echo ok", "cwd": "../outside"})
            return result, approvals
        with TemporaryDirectory() as directory:
            result, approvals = asyncio.run(exercise(directory))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "path_outside_workspace")
            self.assertEqual(approvals, [])

    def test_command_side_effects_are_rejected_before_approval_and_require_write_file(self):
        async def exercise(directory):
            approvals = []
            async def approve(role, command, cwd):
                approvals.append((role, command, cwd))
                return True
            runner = Orchestrator(
                "test", "检查并修复", ToolRegistry(Workspace(directory)), FakeClient(), settings,
                lambda event: asyncio.sleep(0), command_mode="ask", request_command_approval=approve,
            )
            result = await runner._execute(AgentRole.REVIEWER, "run_command", {"command": "python -c \"open('reviewer.txt','w').write('bad')\""})
            return result, approvals, Path(directory, "reviewer.txt").exists()
        with TemporaryDirectory() as directory:
            result, approvals, created = asyncio.run(exercise(directory))
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "command_file_mutation_not_allowed")
            self.assertEqual(approvals, [])
            self.assertFalse(created)
            self.assertTrue(is_file_mutation_command("Path('x').write_text('bad')"))
            self.assertTrue(is_file_mutation_command("if exist songlin.db move /y songlin.db songlin.db.bak"))
            self.assertTrue(is_inspection_command("type main.py"))
            self.assertTrue(is_inspection_command("python -c \"print(open('main.py').read())\""))
    def test_reviewer_verdict_parser_accepts_marker_after_natural_prefix(self):
        passed, detail = parse_reviewer_verdict("已核对九项测试。\nVERDICT: PASS\n全部通过。")
        self.assertTrue(passed)
        self.assertNotIn("VERDICT", detail)
        self.assertIn("全部通过", detail)

    def test_reviewer_verdict_parser_uses_last_marker(self):
        passed, detail = parse_reviewer_verdict("VERDICT: PASS 初检。\nVERDICT: NEEDS_WORK 仍有失败。")
        self.assertFalse(passed)
        self.assertIn("仍有失败", detail)

    def test_reviewer_rejection_requires_a_traceable_basis(self):
        self.assertEqual(parse_reviewer_basis("VERDICT: NEEDS_WORK\nBASIS: USER_REQUIREMENT\nEVIDENCE: x\nACTION: y"), "USER_REQUIREMENT")
        self.assertEqual(parse_reviewer_basis("VERDICT: NEEDS_WORK 因为我觉得还不够好"), "")

    def test_risky_command_is_blocked(self):
        tools = ToolRegistry(Workspace(r"C:\Desktop\NJUSE_AgentProj"))
        self.assertEqual(tools.run_command("rm -rf temp").code, "command_requires_approval")

    def test_background_server_command_is_blocked(self):
        tools = ToolRegistry(Workspace(r"C:\Desktop\NJUSE_AgentProj"))
        self.assertEqual(tools.run_command("start /b python -m uvicorn app:app").code, "background_command_not_allowed")
        self.assertEqual(tools.run_command("taskkill /F /PID 1234").code, "command_requires_approval")

    def test_workspace_path_escape_is_rejected(self):
        with TemporaryDirectory() as directory:
            result = ToolRegistry(Workspace(directory)).read_file("../outside.txt")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "path_outside_workspace")

    def test_write_file_returns_unified_diff(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "note.txt"
            target.write_text("before\n", encoding="utf-8")
            result = ToolRegistry(Workspace(directory)).write_file("note.txt", "after\n", content_sha256("before\n"))
            self.assertTrue(result.ok)
            self.assertIn("-before", result.meta["diff"])
            self.assertIn("+after", result.meta["diff"])

    def test_existing_file_cannot_be_overwritten_without_read_version(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "index.html"
            target.write_text("<html><body>完整页面</body></html>\n", encoding="utf-8")
            result = ToolRegistry(Workspace(directory)).write_file("index.html", "function buildQuery() {}")
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "file_changed")
            self.assertIn("完整页面", target.read_text(encoding="utf-8"))

    def test_replace_text_applies_one_version_checked_edit(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "index.html"
            before = 'p.set("sort_by", parts[0]);\n'
            target.write_text(before, encoding="utf-8")
            tools = ToolRegistry(Workspace(directory))
            result = tools.replace_text(
                "index.html",
                'parts[0]',
                'sortMap[value].sort_by',
                content_sha256(before),
            )
            self.assertTrue(result.ok)
            self.assertIn('sortMap[value].sort_by', target.read_text(encoding="utf-8"))
            self.assertIn("+p.set", result.meta["diff"])

    def test_latest_command_failure_is_not_hidden_by_an_earlier_success(self):
        async def exercise(directory):
            runner = Orchestrator("test", "执行并修复", ToolRegistry(Workspace(directory)), FakeClient(), settings, lambda event: asyncio.sleep(0), task_analysis=COMMAND_ANALYSIS)
            success = await runner._execute(AgentRole.CODER, "run_command", {"command": "echo ok"})
            blocked = await runner._execute(AgentRole.CODER, "run_command", {"command": "python -c \"open('x.txt','w').write('x')\""})
            return runner, success, blocked
        with TemporaryDirectory() as directory:
            runner, success, blocked = asyncio.run(exercise(directory))
            self.assertTrue(success.ok)
            self.assertEqual(blocked.code, "command_file_mutation_not_allowed")
            self.assertFalse(runner._has_successful_command())

    def test_workspace_tree_lists_nested_files_and_skips_dependencies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "nested").mkdir(parents=True)
            (root / "src" / "nested" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "hidden.js").write_text("ignored", encoding="utf-8")
            result = ToolRegistry(Workspace(directory)).workspace_tree()
            paths = {item["path"] for item in result.meta["items"]}
            self.assertTrue(result.ok)
            self.assertIn("src", paths)
            self.assertIn("src/nested/app.py", paths)
            self.assertNotIn("node_modules", paths)
            self.assertNotIn("node_modules/hidden.js", paths)

    def test_search_text_accepts_a_direct_file_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "page.html").write_text("<title>松林任务台</title>\n", encoding="utf-8")
            result = ToolRegistry(Workspace(directory)).search_text("松林任务台", "page.html")
            self.assertTrue(result.ok)
            self.assertEqual(result.meta["matches"][0]["path"], "page.html")
            self.assertEqual(result.meta["matches"][0]["line"], 1)

    def test_reviewer_rejection_enters_a_bounded_repair_cycle(self):
        async def exercise(directory):
            events, client = [], RepairingWorkflowClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "创建并验证 result.txt，内容必须为 correct",
                ToolRegistry(Workspace(directory)), client, settings, publish, task_analysis=CHANGE_ANALYSIS,
            )
            completed = await runner.run()
            return completed, runner, events, client

        with TemporaryDirectory() as directory:
            completed, runner, events, client = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual((Path(directory) / "result.txt").read_text(encoding="utf-8"), "correct\n")
            self.assertEqual(runner.repair_cycles, 1)
            self.assertEqual(client.reviewer_calls, 2)
            self.assertIn("exit_code=0", client.reviewer_inputs[0])
            self.assertIn("Frozen acceptance scope", client.reviewer_inputs[0])
            self.assertIn("Only open issue from the previous review", client.reviewer_inputs[1])
            self.assertIn("BASIS: USER_REQUIREMENT", client.reviewer_inputs[1])
            self.assertIn("ACTION:", client.reviewer_inputs[1])
            self.assertTrue(any(event.payload.get("repairing") for event in events))
            self.assertEqual(events[-1].type, "task_finished")
            self.assertEqual(events[-1].payload["repair_cycles"], 1)
            self.assertFalse(any(event.type == "task_failed" for event in events))

    def test_short_retry_inherits_the_previous_engineering_task_and_can_verify_retained_files(self):
        async def exercise(directory):
            events, client = [], ResumeExistingClient()
            async def publish(event): events.append(event)
            (Path(directory) / "existing.txt").write_text("ready\n", encoding="utf-8")
            runner = Orchestrator(
                "test", "重试", ToolRegistry(Workspace(directory)), client, settings, publish,
                conversation_context="第2轮（failed） 用户：创建并验证 existing.txt 结果：实现者达到轮次上限。",
                reference_task="创建并验证 existing.txt，内容必须为 ready",
                resume_existing=True,
                task_analysis=CONTINUATION_CHANGE_ANALYSIS,
            )
            completed = await runner.run()
            return completed, runner, events, client

        with TemporaryDirectory() as directory:
            completed, runner, events, client = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertTrue(runner.requires_change)
            self.assertEqual(runner.changed_files, set())
            self.assertIn("Referenced earlier task", client.reviewer_context)
            self.assertIn("exit_code=0", client.reviewer_context)
            self.assertEqual(events[-1].type, "task_finished")

    def test_resumed_coder_turn_limit_hands_off_to_reviewer_instead_of_failing(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "继续完成上面的要求", ToolRegistry(Workspace(directory)), TurnLimitedResumeClient(), settings, publish,
                reference_task="创建并验证项目文件", resume_existing=True,
                agent_config={"coder": {"max_turns": 2}},
                task_analysis=CONTINUATION_CHANGE_ANALYSIS,
            )
            return await runner.run(), runner, events

        with TemporaryDirectory() as directory:
            completed, runner, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(runner.failure_reason, "")
            self.assertTrue(any(event.role == AgentRole.CODER and event.payload.get("reason") == "turn_limit" for event in events))
            self.assertEqual(events[-1].type, "task_finished")

    def test_repair_inspection_budget_removes_read_tools_and_preserves_write_budget(self):
        async def exercise(directory):
            events, client = [], InspectionHeavyRepairClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "创建并验证 result.txt", ToolRegistry(Workspace(directory)), client, settings, publish,
                agent_config={"coder": {"max_turns": 10}},
                task_analysis=CHANGE_ANALYSIS,
            )
            return await runner.run(), runner, client, events
        with TemporaryDirectory() as directory:
            completed, runner, client, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(client.repair_reads, 3, client.repair_tool_names)
            self.assertEqual(runner.repair_cycles, 1)
            self.assertEqual((Path(directory) / "result.txt").read_text(encoding="utf-8"), "fixed\n")
            self.assertEqual(events[-1].type, "task_finished")

    def test_orchestrator_runs_all_roles(self):
        async def exercise():
            events = []

            async def publish(event):
                events.append(event)

            tools = ToolRegistry(Workspace(r"C:\Desktop\NJUSE_AgentProj"))
            await Orchestrator("test", "检查项目", tools, FakeClient(), settings, publish).run()
            return events

        events = asyncio.run(exercise())
        self.assertEqual(len(events), 9)
        self.assertEqual(events[-1].type, "task_finished")

    def test_repeated_tool_calls_do_not_fail_entire_task(self):
        async def exercise():
            events = []

            async def publish(event):
                events.append(event)

            tools = ToolRegistry(Workspace(r"C:\Desktop\NJUSE_AgentProj"))
            await Orchestrator("test", "检查项目", tools, RepeatingToolClient(), settings, publish).run()
            return events

        events = asyncio.run(exercise())
        self.assertEqual(events[-1].type, "task_finished")
        self.assertTrue(any(event.payload.get("result", {}).get("code") == "repeated_tool_call" for event in events))
        self.assertTrue(any("已停止重复调用" in event.summary for event in events))

    def test_command_roles_receive_exact_python_interpreter(self):
        async def exercise(directory):
            client = CapturingClient()

            async def publish(_event):
                return None

            await Orchestrator("test", "检查项目", ToolRegistry(Workspace(directory)), client, settings, publish).run()
            return client.system_messages

        with TemporaryDirectory() as directory:
            system_messages = asyncio.run(exercise(directory))
            self.assertNotIn("exact interpreter", system_messages[0])
            self.assertNotIn("exact interpreter", system_messages[1])
            self.assertIn("exact interpreter", system_messages[2])
            self.assertIn("exact interpreter", system_messages[3])

    def test_successful_write_is_reported_in_completion_event(self):
        async def exercise(directory):
            events = []

            async def publish(event):
                events.append(event)

            tools = ToolRegistry(Workspace(directory))
            await Orchestrator("test", "创建文件", tools, WritingClient(), settings, publish, task_analysis=CHANGE_ANALYSIS).run()
            return events

        with TemporaryDirectory() as directory:
            events = asyncio.run(exercise(directory))
            self.assertEqual((Path(directory) / "created.txt").read_text(encoding="utf-8"), "created by MossCode\n")
            self.assertEqual(events[-1].payload["changed_files"], ["created.txt"])

    def test_change_task_without_write_is_failed_not_completed(self):
        async def exercise(directory):
            events = []

            async def publish(event):
                events.append(event)

            completed = await Orchestrator("test", "创建一个文件", ToolRegistry(Workspace(directory)), NeverWritesClient(), settings, publish, task_analysis=CHANGE_ANALYSIS).run()
            return completed, events

        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertFalse(completed)
            self.assertEqual(events[-1].type, "task_failed")
            self.assertFalse(any(event.type == "task_finished" for event in events))

    def test_read_only_task_with_modify_negation_does_not_require_write(self):
        async def exercise(directory):
            events = []

            async def publish(event):
                events.append(event)

            completed = await Orchestrator("test", "只读检查，不要修改文件", ToolRegistry(Workspace(directory)), NeverWritesClient(), settings, publish).run()
            return completed, events

        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(events[-1].type, "task_finished")
            self.assertIn("只读检查完成", events[-1].summary)

    def test_reviewer_rejection_prevents_false_completion(self):
        async def exercise(directory):
            events = []

            async def publish(event):
                events.append(event)

            runner = Orchestrator("test", "只读检查，不要修改文件", ToolRegistry(Workspace(directory)), RejectingReviewerClient(), settings, publish)
            completed = await runner.run()
            return completed, runner.failure_reason, events

        with TemporaryDirectory() as directory:
            completed, reason, events = asyncio.run(exercise(directory))
            self.assertFalse(completed)
            self.assertEqual(reason, "verification_incomplete_after_repairs")
            self.assertEqual(events[-1].type, "task_failed")
            self.assertFalse(any(event.type == "task_finished" for event in events))

    def test_single_agent_is_a_real_verified_execution_path(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator("test", "创建并验证文件", ToolRegistry(Workspace(directory)), StructuredEvidenceClient(), settings, publish, execution_mode="single", task_analysis=CHANGE_ANALYSIS)
            return await runner.run(), events
        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            started_roles = [event.role for event in events if event.type == "agent_started"]
            self.assertEqual(started_roles, [AgentRole.SINGLE])
            self.assertEqual(events[-1].payload["execution_mode"], "single")

    def test_multi_agent_roles_can_be_disabled_but_coder_is_forced_on(self):
        config = normalize_agent_config({
            "planner": {"enabled": False}, "explorer": {"enabled": False},
            "coder": {"enabled": False, "max_turns": 30, "instruction": "优先运行测试"},
            "reviewer": {"enabled": False},
        })
        self.assertTrue(config["coder"]["enabled"])
        self.assertEqual(config["coder"]["instruction"], "优先运行测试")

    def test_adaptive_mode_uses_the_api_analysis_choice(self):
        self.assertEqual(resolve_agent_mode("adaptive", TaskAnalysis(adaptive_mode="single")), "single")
        self.assertEqual(resolve_agent_mode("adaptive", TaskAnalysis(adaptive_mode="multi")), "multi")
        self.assertEqual(resolve_agent_mode("single", TaskAnalysis(adaptive_mode="multi")), "single")

    def test_identity_question_keeps_selected_mode_tools_and_honors_preference_memory(self):
        async def exercise(directory):
            events, client = [], ConversationalMemoryClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "你是谁？", ToolRegistry(Workspace(directory)), client, settings, publish,
                conversation_context="同一工作区的用户偏好：用户要求称呼助手为小m。", execution_mode="single",
                memory_metadata={"cross_session_enabled": True, "shared_memory_loaded": True, "preference_count": 1},
            )
            return await runner.run(), events, client
        with TemporaryDirectory() as directory:
            completed, events, client = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual([event.role for event in events if event.type == "agent_started"], [AgentRole.SINGLE])
            self.assertIn("run_command", {tool["function"]["name"] for tool in client.calls[0][1]})
            self.assertIn("小m", events[-1].summary)
            self.assertNotIn("只读检查", events[-1].summary)
            self.assertEqual(events[-1].payload["intent"], "general")
            self.assertEqual(events[0].payload["preference_count"], 1)

    def test_start_request_executes_a_real_command_and_hides_serialized_tool_markup(self):
        async def exercise(directory):
            events, client = [], SerializedToolRecoveryClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "帮我启动一下服务，避开 8000 端口", ToolRegistry(Workspace(directory)),
                client, settings, publish, execution_mode="single", task_analysis=COMMAND_ANALYSIS,
            )
            return await runner.run(), runner, events

        with TemporaryDirectory() as directory:
            completed, runner, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertTrue(runner.requires_command)
            self.assertTrue(any(event.type == "tool_finished" and event.payload.get("tool") == "run_command" for event in events))
            self.assertFalse(any("DSML" in event.summary for event in events))
            self.assertEqual(events[-1].payload["completion_source"], "structured_evidence")

    def test_continuation_comes_from_api_analysis_not_keywords(self):
        with TemporaryDirectory() as directory:
            runner = Orchestrator(
                "test", "啥玩意，你继续了吗", ToolRegistry(Workspace(directory)), FakeClient(), settings,
                lambda event: asyncio.sleep(0), reference_task="修复登录", task_analysis=TaskAnalysis(continuation=True),
            )
            self.assertTrue(runner.is_continuation)
            self.assertIn("修复登录", runner.effective_task)


if __name__ == "__main__":
    unittest.main()
