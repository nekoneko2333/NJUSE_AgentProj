from __future__ import annotations

import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.models import AgentEvent, CheckpointSnapshot, ConversationTurn, ExecutionSnapshot, SessionListItem, SessionSnapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    def __init__(self, database_path: str) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    task TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    status TEXT NOT NULL,
                    memory_summary TEXT NOT NULL DEFAULT '',
                    command_mode TEXT NOT NULL DEFAULT 'auto',
                    cross_session_memory_enabled INTEGER NOT NULL DEFAULT 0,
                    agent_mode TEXT NOT NULL DEFAULT 'multi',
                    agent_config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, position)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    turn_id TEXT REFERENCES turns(id) ON DELETE CASCADE,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, position);
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_executions_session ON executions(session_id, created_at);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id, created_at);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()}
            if "command_mode" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN command_mode TEXT NOT NULL DEFAULT 'auto'")
            if "cross_session_memory_enabled" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN cross_session_memory_enabled INTEGER NOT NULL DEFAULT 0")
            if "agent_mode" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'multi'")
            if "agent_config_json" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN agent_config_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("UPDATE sessions SET status = 'interrupted' WHERE status = 'running'")
            connection.execute("UPDATE turns SET status = 'interrupted' WHERE status = 'running'")
            connection.execute("UPDATE executions SET status = 'interrupted',reason = 'application_restarted',updated_at=? WHERE status IN ('queued','running','waiting_approval','cancel_requested')", (utc_now(),))

    def create_session(self, *, task: str, workspace: str, locale: str, command_mode: str = "auto", cross_session_memory_enabled: bool = False, agent_mode: str = "multi", agent_config: dict | None = None) -> tuple[SessionSnapshot, ConversationTurn]:
        now, session_id, turn_id = utc_now(), str(uuid4()), str(uuid4())
        title = task.strip().replace("\n", " ")[:48]
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO sessions(id,title,task,workspace,locale,status,command_mode,cross_session_memory_enabled,agent_mode,agent_config_json,created_at,updated_at) VALUES(?,?,?,?,?,'created',?,?,?,?,?,?)",
                (session_id, title, task, workspace, locale, command_mode, int(cross_session_memory_enabled), agent_mode, json.dumps(agent_config or {}, ensure_ascii=False), now, now),
            )
            connection.execute(
                "INSERT INTO turns(id,session_id,position,user_content,status,created_at) VALUES(?,?,1,?,'pending',?)",
                (turn_id, session_id, task, now),
            )
        return self.get_session(session_id), self.get_turn(turn_id)

    def append_turn(self, session_id: str, task: str, locale: str) -> ConversationTurn | None:
        now, turn_id = utc_now(), str(uuid4())
        with self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(position),0)+1 AS position FROM turns WHERE session_id=?", (session_id,)).fetchone()
            if not connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                return None
            position = int(row["position"])
            connection.execute(
                "INSERT INTO turns(id,session_id,position,user_content,status,created_at) VALUES(?,?,?,?, 'pending', ?)",
                (turn_id, session_id, position, task, now),
            )
            connection.execute(
                "UPDATE sessions SET task=?,locale=?,status=CASE WHEN status='running' THEN 'running' ELSE 'created' END,updated_at=? WHERE id=?",
                (task, locale, now, session_id),
            )
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> ConversationTurn:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return ConversationTurn(**dict(row))

    def list_turns(self, session_id: str) -> list[ConversationTurn]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM turns WHERE session_id=? ORDER BY position", (session_id,)).fetchall()
        return [ConversationTurn(**dict(row)) for row in rows]

    def pending_turn(self, session_id: str) -> ConversationTurn | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM turns WHERE session_id=? AND status IN ('pending','interrupted') ORDER BY position ASC LIMIT 1", (session_id,)).fetchone()
        return ConversationTurn(**dict(row)) if row else None

    def update_turn(self, turn_id: str, *, status: str, assistant_summary: str = "") -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("UPDATE turns SET status=?,assistant_summary=? WHERE id=?", (status, assistant_summary, turn_id))
            connection.execute("UPDATE sessions SET status=?,updated_at=? WHERE id=(SELECT session_id FROM turns WHERE id=?)", ("finished" if status == "finished" else status, now, turn_id))

    def update_session_status(self, session_id: str, status: str) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE sessions SET status=?,updated_at=? WHERE id=?", (status, utc_now(), session_id))

    def update_command_mode(self, session_id: str, command_mode: str) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE sessions SET command_mode=?,updated_at=? WHERE id=?", (command_mode, utc_now(), session_id))

    def update_cross_session_memory(self, session_id: str, enabled: bool) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE sessions SET cross_session_memory_enabled=?,updated_at=? WHERE id=?", (int(enabled), utc_now(), session_id))

    def update_agent_workflow(self, session_id: str, agent_mode: str, agent_config: dict) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE sessions SET agent_mode=?,agent_config_json=?,updated_at=? WHERE id=?",
                (agent_mode, json.dumps(agent_config, ensure_ascii=False), utc_now(), session_id),
            )

    @staticmethod
    def _workspace_key(workspace: str) -> str:
        return os.path.normcase(os.path.abspath(os.path.normpath(workspace)))

    def workspace_memory(self, workspace: str, exclude_session_id: str, limit: int = 12) -> str:
        target = self._workspace_key(workspace)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT s.id,s.title,s.workspace,t.position,t.user_content,t.assistant_summary FROM turns t JOIN sessions s ON s.id=t.session_id WHERE s.id<>? AND t.status='finished' ORDER BY s.updated_at DESC,t.position DESC LIMIT 100",
                (exclude_session_id,),
            ).fetchall()
        matches = [row for row in rows if self._workspace_key(row["workspace"]) == target][:max(1, limit)]
        matches.reverse()
        return "\n".join(
            f"[{row['title']} · 第{row['position']}轮] 用户：{' '.join(row['user_content'].split())[:320]} 结果：{' '.join(row['assistant_summary'].split())[:620]}"
            for row in matches
        )

    def workspace_preferences(self, workspace: str, _session_id: str, limit: int = 8) -> list[str]:
        """Return live workspace preferences with the newest naming statement taking precedence.

        Completed turns from the current session are included intentionally: a rename made in
        another conversation must supersede an older name still present in this conversation.
        """
        target = self._workspace_key(workspace)
        name_markers = ("称呼你", "叫你", "你叫", "改名", "名字是", "名字叫", "call you", "your name is", "rename you")
        preference_markers = name_markers + ("称呼", "叫我", "我叫", "记住", "偏好", "以后", "请使用", "不要叫", "call me", "remember", "prefer")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT s.id,s.workspace,t.user_content,t.created_at FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.status='finished' ORDER BY t.created_at DESC LIMIT 200",
            ).fetchall()
        preferences: list[str] = []
        current_name_found = False
        for row in rows:
            content = " ".join(row["user_content"].split()).strip()
            normalized = content.lower()
            if self._workspace_key(row["workspace"]) != target or not any(marker in normalized for marker in preference_markers):
                continue
            is_name_question = "叫什么" in normalized or "what is your name" in normalized or "what's your name" in normalized
            is_name_preference = not is_name_question and (any(marker in normalized for marker in name_markers) or ("称呼" in normalized and "你" in normalized))
            if is_name_preference and current_name_found:
                continue
            if content not in preferences:
                preferences.append(content[:500])
                current_name_found = current_name_found or is_name_preference
            if len(preferences) >= max(1, limit):
                break
        return preferences

    def set_memory_summary(self, session_id: str, summary: str) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE sessions SET memory_summary=?,updated_at=? WHERE id=?", (summary, utc_now(), session_id))

    def add_event(self, event: AgentEvent) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO events(session_id,turn_id,data,created_at) VALUES(?,?,?,?)",
                (event.session_id, event.turn_id, event.model_dump_json(), utc_now()),
            )

    def list_events(self, session_id: str) -> list[AgentEvent]:
        with self._connection() as connection:
            rows = connection.execute("SELECT data FROM events WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [AgentEvent.model_validate_json(row["data"]) for row in rows]

    def get_session(self, session_id: str) -> SessionSnapshot | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        values = dict(row)
        raw_config = values.pop("agent_config_json", "{}")
        try:
            values["agent_config"] = json.loads(raw_config or "{}")
        except (TypeError, json.JSONDecodeError):
            values["agent_config"] = {}
        return SessionSnapshot(**values, turns=self.list_turns(session_id), events=self.list_events(session_id))

    def list_sessions(self, limit: int = 40) -> list[SessionListItem]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT s.id,s.title,s.task,s.workspace,s.locale,s.status,s.updated_at,s.command_mode,s.cross_session_memory_enabled,s.agent_mode,COUNT(t.id) AS turn_count FROM sessions s LEFT JOIN turns t ON t.session_id=s.id GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SessionListItem(**dict(row)) for row in rows]

    def create_execution(self, session_id: str, turn_id: str | None) -> ExecutionSnapshot:
        now, execution_id = utc_now(), str(uuid4())
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO executions(id,session_id,turn_id,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                (execution_id, session_id, turn_id, now, now),
            )
        return self.get_execution(execution_id)

    def get_execution(self, execution_id: str) -> ExecutionSnapshot:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return ExecutionSnapshot(**dict(row))

    def latest_execution(self, session_id: str) -> ExecutionSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return ExecutionSnapshot(**dict(row)) if row else None

    def update_execution(self, execution_id: str, status: str, reason: str = "") -> ExecutionSnapshot:
        with self._connection() as connection:
            connection.execute("UPDATE executions SET status=?,reason=?,updated_at=? WHERE id=?", (status, reason, utc_now(), execution_id))
        return self.get_execution(execution_id)

    def retry_turn(self, turn_id: str) -> ConversationTurn:
        turn = self.get_turn(turn_id)
        if turn.status not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("turn_not_retryable")
        with self._connection() as connection:
            connection.execute("UPDATE turns SET status='pending',assistant_summary='' WHERE id=?", (turn_id,))
            connection.execute("UPDATE sessions SET status='created',updated_at=? WHERE id=?", (utc_now(), turn.session_id))
        return self.get_turn(turn_id)

    def add_checkpoint(self, checkpoint: CheckpointSnapshot) -> None:
        import json
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO checkpoints(id,session_id,turn_id,label,status,files_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (checkpoint.id, checkpoint.session_id, checkpoint.turn_id, checkpoint.label, checkpoint.status, json.dumps(checkpoint.files, ensure_ascii=False), checkpoint.created_at),
            )

    def list_checkpoints(self, session_id: str) -> list[CheckpointSnapshot]:
        import json
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM checkpoints WHERE session_id=? ORDER BY created_at DESC", (session_id,)).fetchall()
        return [CheckpointSnapshot(id=row["id"], session_id=row["session_id"], turn_id=row["turn_id"], label=row["label"], status=row["status"], files=json.loads(row["files_json"]), created_at=row["created_at"]) for row in rows]

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointSnapshot | None:
        import json
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if row is None:
            return None
        return CheckpointSnapshot(id=row["id"], session_id=row["session_id"], turn_id=row["turn_id"], label=row["label"], status=row["status"], files=json.loads(row["files_json"]), created_at=row["created_at"])

    def update_checkpoint_status(self, checkpoint_id: str, status: str) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE checkpoints SET status=? WHERE id=?", (status, checkpoint_id))

    def set_checkpoint_files(self, checkpoint_id: str, files: list[str]) -> None:
        import json
        with self._connection() as connection:
            connection.execute("UPDATE checkpoints SET files_json=? WHERE id=?", (json.dumps(files, ensure_ascii=False), checkpoint_id))
