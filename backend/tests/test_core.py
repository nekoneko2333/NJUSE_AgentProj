import asyncio
import json
import sys
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from app.agents.orchestrator import Orchestrator, is_continuation_task, is_conversational_task, is_file_mutation_command, is_inspection_command, normalize_agent_config, parse_reviewer_basis, parse_reviewer_verdict, resolve_agent_mode
from app.core.settings import settings
from app.core.models import AgentRole
from app.core.auth import LocalAuth
from app.core.settings import Settings
from app.core.checkpoints import CheckpointManager
from app.core.extensions import HookConfig, load_project_rules
from app.storage.sqlite_store import SQLiteStore
from app.runtime.workspace import Workspace
from app.tools.registry import ToolRegistry


class FakeClient:
    async def complete(self, messages, tools):
        if "reviewer agent" in messages[0]["content"]:
            return {"role": "assistant", "content": "VERDICT: PASS 已检查完成"}
        return {"role": "assistant", "content": "已完成"}


class RepeatingToolClient:
    async def complete(self, messages, tools):
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


class ConversationalMemoryClient:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return {"role": "assistant", "content": "我是小m，你的统一 MossCode 助手。"}


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
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"result.txt","content":"correct\\n"}'}}]}
        if repairing and not self.repair_verified:
            self.repair_verified = True
            command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').read_text().strip() == \'correct\'"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
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
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"result.txt","content":"fixed\\n"}'}}]}
        if repairing and not self.repair_verified:
            self.repair_verified = True
            command = f'"{sys.executable}" -c "from pathlib import Path; assert Path(\'result.txt\').read_text().strip() == \'fixed\'"'
            return {"role": "assistant", "content": None, "tool_calls": [{"id": "repair-test", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}
        return {"role": "assistant", "content": "实现已完成。"}


class CoreTests(unittest.TestCase):
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
            result = ToolRegistry(Workspace(directory), checkpoint=manager).write_file("note.txt", "agent\n")
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
            runner = Orchestrator("test", "创建并验证文件", ToolRegistry(Workspace(directory)), StructuredEvidenceClient(), settings, publish)
            return await runner.run(), events
        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(events[-1].payload["completion_source"], "structured_evidence")

    def test_reviewer_cannot_expand_scope_after_successful_verification(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "创建并验证 evidence.txt", ToolRegistry(Workspace(directory)),
                ScopeDriftingReviewerClient(), settings, publish,
            )
            return await runner.run(), runner, events

        with TemporaryDirectory() as directory:
            completed, runner, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(runner.repair_cycles, 0)
            self.assertEqual(events[-1].payload["completion_source"], "structured_evidence")
            self.assertFalse(any(event.type == "task_failed" for event in events))
    def test_local_auth_accepts_configured_credentials_and_unicode_failures(self):
        auth = LocalAuth(Settings(app_username="moss", app_password="安全密码"))
        self.assertIsNone(auth.authenticate("moss", "错误密码"))
        token = auth.authenticate("moss", "安全密码")
        self.assertIsNotNone(token)
        self.assertIn(token, auth.tokens)

    def test_chinese_creation_synonyms_require_a_write(self):
        with TemporaryDirectory() as directory:
            runner = Orchestrator("test", "撰写一个花哨的网页", ToolRegistry(Workspace(directory)), FakeClient(), settings, lambda event: asyncio.sleep(0))
            self.assertTrue(runner.requires_change)

    def test_reviewer_gets_a_tool_free_finalization_turn(self):
        async def exercise(directory):
            events = []
            async def publish(event): events.append(event)
            runner = Orchestrator("test", "只读检查", ToolRegistry(Workspace(directory)), ReviewerFinalizationClient(), settings, publish)
            return await runner.run(), events
        with TemporaryDirectory() as directory:
            completed, events = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertEqual(events[-1].type, "task_finished")
            self.assertTrue(any(event.payload.get("reason") == "forced_summary" for event in events))

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
            result = ToolRegistry(Workspace(directory)).write_file("note.txt", "after\n")
            self.assertTrue(result.ok)
            self.assertIn("-before", result.meta["diff"])
            self.assertIn("+after", result.meta["diff"])

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
                ToolRegistry(Workspace(directory)), client, settings, publish,
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
            )
            completed = await runner.run()
            return completed, runner, events, client

        with TemporaryDirectory() as directory:
            completed, runner, events, client = asyncio.run(exercise(directory))
            self.assertTrue(is_continuation_task("继续完成我上面的要求"))
            self.assertTrue(completed)
            self.assertFalse(runner.conversation_only)
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
            await Orchestrator("test", "创建文件", tools, WritingClient(), settings, publish).run()
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

            completed = await Orchestrator("test", "创建一个文件", ToolRegistry(Workspace(directory)), NeverWritesClient(), settings, publish).run()
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
            self.assertIn("只读检查已完成", events[-1].summary)

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
            runner = Orchestrator("test", "创建并验证文件", ToolRegistry(Workspace(directory)), StructuredEvidenceClient(), settings, publish, execution_mode="single")
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

    def test_adaptive_mode_records_a_deterministic_choice(self):
        self.assertEqual(resolve_agent_mode("解释这个文件", "adaptive"), "single")
        self.assertEqual(resolve_agent_mode("重构前后端架构并补充测试", "adaptive"), "multi")

    def test_identity_question_bypasses_reviewer_and_honors_preference_memory(self):
        async def exercise(directory):
            events, client = [], ConversationalMemoryClient()
            async def publish(event): events.append(event)
            runner = Orchestrator(
                "test", "你是谁？", ToolRegistry(Workspace(directory)), client, settings, publish,
                conversation_context="同一工作区的用户偏好：用户要求称呼助手为小m。", execution_mode="multi",
                memory_metadata={"cross_session_enabled": True, "shared_memory_loaded": True, "preference_count": 1},
            )
            return await runner.run(), events, client
        with TemporaryDirectory() as directory:
            completed, events, client = asyncio.run(exercise(directory))
            self.assertTrue(completed)
            self.assertTrue(is_conversational_task("你是谁？"))
            self.assertFalse(is_conversational_task("检查项目代码"))
            self.assertEqual([event.role for event in events if event.type == "agent_started"], [AgentRole.SINGLE])
            self.assertEqual(client.calls[0][1], [])
            self.assertIn("小m", events[-1].summary)
            self.assertNotIn("只读检查", events[-1].summary)
            self.assertEqual(events[-1].payload["intent"], "conversation")
            self.assertEqual(events[0].payload["preference_count"], 1)


if __name__ == "__main__":
    unittest.main()
