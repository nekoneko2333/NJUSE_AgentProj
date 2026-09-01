import asyncio
import json
import sys
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from app.agents.orchestrator import Orchestrator, parse_reviewer_verdict
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
            return {"role": "assistant", "content": "VERDICT: NEEDS_WORK 测试仍然失败"}
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
    def test_reviewer_verdict_parser_accepts_marker_after_natural_prefix(self):
        passed, detail = parse_reviewer_verdict("已核对九项测试。\nVERDICT: PASS\n全部通过。")
        self.assertTrue(passed)
        self.assertNotIn("VERDICT", detail)
        self.assertIn("全部通过", detail)

    def test_reviewer_verdict_parser_uses_last_marker(self):
        passed, detail = parse_reviewer_verdict("VERDICT: PASS 初检。\nVERDICT: NEEDS_WORK 仍有失败。")
        self.assertFalse(passed)
        self.assertIn("仍有失败", detail)

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
            self.assertEqual(reason, "reviewer_rejected")
            self.assertEqual(events[-1].type, "task_failed")
            self.assertFalse(any(event.type == "task_finished" for event in events))


if __name__ == "__main__":
    unittest.main()
