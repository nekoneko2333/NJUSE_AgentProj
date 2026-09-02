import asyncio
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from app.core.context import ContextManager
from app.core.models import AgentEvent, AppendTurnRequest, ConversationTurn, EventType, ProjectConfigWriteRequest
from app.storage.sqlite_store import SQLiteStore


class StorageContextTests(unittest.TestCase):
    def test_execution_state_survives_reopen_and_supports_retry(self):
        with TemporaryDirectory() as directory:
            database = str(Path(directory) / "execution.db")
            store = SQLiteStore(database)
            session, turn = store.create_session(task="run", workspace=directory, locale="zh-CN")
            execution = store.create_execution(session.id, turn.id)
            store.update_execution(execution.id, "running")
            reopened = SQLiteStore(database)
            self.assertEqual(reopened.get_execution(execution.id).status, "interrupted")
            self.assertEqual(reopened.latest_execution(session.id).id, execution.id)
            reopened.update_turn(turn.id, status="failed", assistant_summary="failed")
            self.assertEqual(reopened.retry_turn(turn.id).status, "pending")
    def test_sqlite_session_survives_store_recreation_and_appends_turn(self):
        with TemporaryDirectory() as directory:
            database = str(Path(directory) / "mosscode.db")
            store = SQLiteStore(database)
            session, turn = store.create_session(task="创建说明文件", workspace=directory, locale="zh-CN")
            store.add_event(AgentEvent(type=EventType.TASK_CREATED, session_id=session.id, turn_id=turn.id, summary="任务已创建", payload={"task": turn.user_content, "position": 1}))
            store.update_turn(turn.id, status="finished", assistant_summary="已创建 README。")

            restored = SQLiteStore(database).get_session(session.id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.turns[0].assistant_summary, "已创建 README。")
            self.assertEqual(restored.events[0].payload["task"], "创建说明文件")

            follow_up = SQLiteStore(database).append_turn(session.id, "继续补充示例", "zh-CN")
            self.assertIsNotNone(follow_up)
            self.assertEqual(follow_up.position, 2)
            self.assertEqual(SQLiteStore(database).list_sessions()[0].turn_count, 2)

    def test_command_mode_is_persisted_and_can_be_changed(self):
        with TemporaryDirectory() as directory:
            database = str(Path(directory) / "permissions.db")
            store = SQLiteStore(database)
            session, _ = store.create_session(task="测试权限", workspace=directory, locale="zh-CN", command_mode="ask")
            self.assertEqual(store.get_session(session.id).command_mode, "ask")
            store.update_command_mode(session.id, "deny")
            self.assertEqual(SQLiteStore(database).get_session(session.id).command_mode, "deny")

    def test_cross_session_memory_is_opt_in_and_workspace_scoped(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as other_directory:
            store = SQLiteStore(str(Path(directory) / "memory.db"))
            source, source_turn = store.create_session(task="项目暗号是什么", workspace=directory, locale="zh-CN")
            store.update_turn(source_turn.id, status="finished", assistant_summary="项目暗号是苔藓42。")
            unrelated, unrelated_turn = store.create_session(task="另一个项目", workspace=other_directory, locale="zh-CN")
            store.update_turn(unrelated_turn.id, status="finished", assistant_summary="不应被读取。")
            current, _ = store.create_session(task="继续工作", workspace=directory, locale="zh-CN", cross_session_memory_enabled=True)

            restored = store.get_session(current.id)
            self.assertTrue(restored.cross_session_memory_enabled)
            shared = store.workspace_memory(directory, current.id)
            self.assertIn("苔藓42", shared)
            self.assertNotIn("不应被读取", shared)
            window = ContextManager().build([], shared_memory=shared)
            self.assertIn("其他对话记忆", window.text)

            store.update_cross_session_memory(current.id, False)
            self.assertFalse(SQLiteStore(str(Path(directory) / "memory.db")).get_session(current.id).cross_session_memory_enabled)

    def test_workspace_preferences_extracts_names_only_from_same_workspace(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as other_directory:
            store = SQLiteStore(str(Path(directory) / "preferences.db"))
            source, source_turn = store.create_session(task="以后称呼你为小m，请记住", workspace=directory, locale="zh-CN")
            store.update_turn(source_turn.id, status="finished", assistant_summary="小m收到。")
            other, other_turn = store.create_session(task="以后叫你别的名字", workspace=other_directory, locale="zh-CN")
            store.update_turn(other_turn.id, status="finished", assistant_summary="收到。")
            current, _ = store.create_session(task="你是谁", workspace=directory, locale="zh-CN", cross_session_memory_enabled=True)
            preferences = store.workspace_preferences(directory, current.id)
            self.assertEqual(preferences, ["以后称呼你为小m，请记住"])
            window = ContextManager().build([], shared_preferences="\n".join(preferences))
            self.assertIn("必须遵循", window.text)
            self.assertIn("小m", window.text)

    def test_latest_workspace_rename_overrides_an_older_name_in_the_current_session(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(str(Path(directory) / "rename.db"))
            current, old_turn = store.create_session(task="以后称呼你为小m，请记住", workspace=directory, locale="zh-CN", cross_session_memory_enabled=True)
            store.update_turn(old_turn.id, status="finished", assistant_summary="小m收到。")
            other, rename_turn = store.create_session(task="你现在改名为小s了", workspace=directory, locale="zh-CN", cross_session_memory_enabled=True)
            store.update_turn(rename_turn.id, status="finished", assistant_summary="以后叫我小s。")
            question = store.append_turn(current.id, "你现在叫什么？", "zh-CN")
            store.update_turn(question.id, status="finished", assistant_summary="旧版本答错为小m。")

            preferences = store.workspace_preferences(directory, current.id)
            self.assertEqual(preferences, ["你现在改名为小s了"])
            window = ContextManager().build(store.list_turns(current.id), shared_preferences="\n".join(preferences))
            self.assertIn("每类第一条是当前值", window.text)
            self.assertIn("小s", window.text)
            self.assertIn("小m", window.text)  # Older conversation remains available but is explicitly lower priority.

    def test_context_window_summarizes_old_turns_and_keeps_recent_turns(self):
        turns = [
            ConversationTurn(id=str(index), session_id="s", position=index, user_content=f"用户问题 {index}", assistant_summary=f"执行结果 {index}", status="finished", created_at="now")
            for index in range(1, 6)
        ]
        window = ContextManager(budget_chars=3000, recent_turns=2, summary_chars=1000).build(turns)
        self.assertIn("第1轮", window.memory_summary)
        self.assertIn("第3轮", window.memory_summary)
        self.assertNotIn("第4轮", window.memory_summary)
        self.assertIn("第4轮", window.text)
        self.assertIn("第5轮", window.text)
        self.assertLessEqual(len(window.text), 3000)
        self.assertGreater(window.estimated_tokens, 0)

    def test_context_keeps_failed_turns_for_retry_references(self):
        turns = [
            ConversationTurn(id="1", session_id="s", position=1, user_content="你好", assistant_summary="你好", status="finished", created_at="now"),
            ConversationTurn(id="2", session_id="s", position=2, user_content="创建松林任务台", assistant_summary="实现者达到轮次上限", status="failed", created_at="now"),
            ConversationTurn(id="3", session_id="s", position=3, user_content="重试", assistant_summary="", status="running", created_at="now"),
        ]
        window = ContextManager(recent_turns=3).build(turns)
        self.assertIn("创建松林任务台", window.text)
        self.assertIn("failed", window.text)
        self.assertNotIn("第3轮", window.text)

    def test_role_message_window_drops_old_tool_messages(self):
        manager = ContextManager(budget_chars=2000)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "current task"},
            {"role": "assistant", "content": "old", "tool_calls": [{"id": "old"}]},
            {"role": "tool", "tool_call_id": "old", "content": "x" * 1800},
            {"role": "assistant", "content": "latest", "tool_calls": [{"id": "latest"}]},
            {"role": "tool", "tool_call_id": "latest", "content": "y" * 900},
        ]
        trimmed = manager.trim_role_messages(messages)
        self.assertEqual(trimmed[:2], messages[:2])
        self.assertLess(len(trimmed), len(messages))
        self.assertEqual(trimmed[-1], messages[-1])
        self.assertEqual(trimmed[-2], messages[-2])

    def test_turns_queued_while_running_keep_fifo_order(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(str(Path(directory) / "queue.db"))
            session, first = store.create_session(task="第一条", workspace=directory, locale="zh-CN")
            store.update_session_status(session.id, "running")
            second = store.append_turn(session.id, "第二条", "zh-CN")
            third = store.append_turn(session.id, "第三条", "zh-CN")
            self.assertIsNotNone(second)
            self.assertIsNotNone(third)
            self.assertEqual(store.get_session(session.id).status, "running")
            self.assertEqual(store.pending_turn(session.id).id, first.id)
            store.update_turn(first.id, status="finished", assistant_summary="完成")
            self.assertEqual(store.pending_turn(session.id).id, second.id)

    def test_append_api_accepts_an_interjection_while_running(self):
        from app import main

        with TemporaryDirectory() as directory:
            original_store = main.store
            try:
                main.store = SQLiteStore(str(Path(directory) / "api-queue.db"))
                session, _ = main.store.create_session(task="正在执行", workspace=directory, locale="zh-CN")
                main.store.update_session_status(session.id, "running")
                snapshot = asyncio.run(main.append_turn(session.id, AppendTurnRequest(task="补充要求", locale="zh-CN")))
                self.assertEqual(snapshot.status, "running")
                self.assertEqual([turn.user_content for turn in snapshot.turns], ["正在执行", "补充要求"])
                self.assertEqual(snapshot.turns[-1].status, "pending")
            finally:
                main.store = original_store

    def test_project_config_api_creates_validated_workspace_files(self):
        from app import main

        with TemporaryDirectory() as directory:
            original_store, original_checkpoints = main.store, main.checkpoint_root
            try:
                main.store = SQLiteStore(str(Path(directory) / "config.db"))
                main.checkpoint_root = Path(directory) / "checkpoints"
                session, _ = main.store.create_session(task="配置项目", workspace=directory, locale="zh-CN")
                payload = ProjectConfigWriteRequest(kind="hooks", content='{"enabled":false,"hooks":{"after_write":[]}}', expected_sha256="")
                result = asyncio.run(main.update_project_config(session.id, payload))
                self.assertTrue(result["hooks"]["configured"])
                self.assertEqual((Path(directory) / ".mosscode" / "hooks.json").read_text(encoding="utf-8"), payload.content)
                self.assertEqual(len(main.store.list_checkpoints(session.id)), 1)
            finally:
                main.store, main.checkpoint_root = original_store, original_checkpoints

    def test_agent_workflow_survives_database_reopen(self):
        with TemporaryDirectory() as directory:
            database = str(Path(directory) / "workflow.db")
            store = SQLiteStore(database)
            config = {"planner": {"enabled": False, "max_turns": 2, "instruction": "简洁规划"}}
            session, _ = store.create_session(task="对照执行", workspace=directory, locale="zh-CN", agent_mode="single", agent_config=config)
            restored = SQLiteStore(database).get_session(session.id)
            self.assertEqual(restored.agent_mode, "single")
            self.assertEqual(restored.agent_config["planner"]["instruction"], "简洁规划")
            store.update_agent_workflow(session.id, "adaptive", {"coder": {"enabled": True, "max_turns": 7, "instruction": ""}})
            self.assertEqual(store.get_session(session.id).agent_mode, "adaptive")


if __name__ == "__main__":
    unittest.main()
