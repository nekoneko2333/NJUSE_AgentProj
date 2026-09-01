import asyncio
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from app.core.context import ContextManager
from app.core.models import AgentEvent, AppendTurnRequest, ConversationTurn, EventType
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


if __name__ == "__main__":
    unittest.main()
