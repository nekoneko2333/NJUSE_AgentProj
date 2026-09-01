from __future__ import annotations

import subprocess
import difflib
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.runtime.workspace import Workspace, WorkspaceError
from app.core.checkpoints import CheckpointManager, content_sha256
from app.core.extensions import HookConfig, MCPManager
from app.core.models import AgentRole

SKIP_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv"}
MAX_OUTPUT = 12_000
MAX_FILE_BYTES = 160_000
RISKY_COMMAND_TERMS = ("rm -rf", "del /", "format ", "shutdown", "restart-computer", "remove-item -recurse", "git reset --hard", "taskkill", "stop-process", "pkill ", "killall ")
BACKGROUND_COMMAND_TERMS = ("start /b", "start-process", "nohup ", "disown", "setsid ")


@dataclass
class ToolResult:
    ok: bool
    code: str
    content: str
    meta: dict[str, Any]


class ToolRegistry:
    def __init__(self, workspace: Workspace, command_timeout_seconds: int = 30, checkpoint: CheckpointManager | None = None, cancel_event: threading.Event | None = None) -> None:
        self.workspace = workspace
        self.command_timeout_seconds = command_timeout_seconds
        self.checkpoint = checkpoint
        self.cancel_event = cancel_event
        self.hooks = HookConfig(workspace.root)
        self.mcp = MCPManager(workspace.root, min(command_timeout_seconds, 15))

    def hook_commands(self, event: str) -> list[str]:
        return self.hooks.commands(event)

    def mcp_schemas(self, role: AgentRole) -> list[dict[str, Any]]:
        return self.mcp.schemas_for(role)

    def call_mcp(self, role: AgentRole, name: str, arguments: dict[str, Any]) -> ToolResult:
        ok, content, meta = self.mcp.call(name, arguments, role)
        return ToolResult(ok, "ok" if ok else content, content if ok else "", meta)

    def list_files(self, path: str = ".") -> ToolResult:
        try:
            root = self.workspace.resolve(path)
            if not root.is_dir():
                return ToolResult(False, "not_a_directory", "", {})
            items = []
            for entry in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                if entry.name in SKIP_NAMES:
                    continue
                items.append({"path": str(entry.relative_to(self.workspace.root)), "kind": "directory" if entry.is_dir() else "file"})
            return ToolResult(True, "ok", "", {"items": items[:300]})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    def workspace_tree(self, limit: int = 1200) -> ToolResult:
        """递归列出 GUI 文件树；不作为模型工具暴露，避免扩大上下文。"""
        items: list[dict[str, str]] = []
        truncated = False
        try:
            for entry in sorted(self.workspace.root.rglob("*"), key=lambda item: item.as_posix().lower()):
                relative = entry.relative_to(self.workspace.root)
                if any(part in SKIP_NAMES for part in relative.parts):
                    continue
                items.append({"path": relative.as_posix(), "kind": "directory" if entry.is_dir() else "file"})
                if len(items) >= limit:
                    truncated = True
                    break
            return ToolResult(True, "ok", "", {"items": items, "truncated": truncated, "limit": limit})
        except OSError:
            return ToolResult(False, "workspace_scan_failed", "", {"items": items})

    def read_file(self, path: str, start: int = 1, end: int = 400) -> ToolResult:
        try:
            target = self.workspace.resolve(path)
            if not target.is_file():
                return ToolResult(False, "file_not_found", "", {})
            if target.stat().st_size > MAX_FILE_BYTES:
                return ToolResult(False, "file_too_large", "", {"max_bytes": MAX_FILE_BYTES})
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            begin = max(start - 1, 0)
            finish = min(max(end, begin), len(lines))
            numbered = "\n".join(f"{index + 1}: {line}" for index, line in enumerate(lines[begin:finish], begin))
            return ToolResult(True, "ok", numbered, {"total_lines": len(lines), "start": begin + 1, "end": finish})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    def read_file_content(self, path: str) -> ToolResult:
        try:
            target = self.workspace.resolve(path)
            if not target.is_file():
                return ToolResult(False, "file_not_found", "", {})
            size = target.stat().st_size
            if size > MAX_FILE_BYTES:
                with target.open("r", encoding="utf-8", errors="replace") as stream:
                    content = stream.read(MAX_FILE_BYTES)
                return ToolResult(True, "file_too_large", content, {"path": path.replace("\\", "/"), "sha256": content_sha256(content), "bytes": size, "readonly": True, "truncated": True, "max_bytes": MAX_FILE_BYTES})
            content = target.read_text(encoding="utf-8", errors="replace")
            return ToolResult(True, "ok", content, {"path": path.replace("\\", "/"), "sha256": content_sha256(content), "bytes": len(content.encode("utf-8")), "readonly": False, "truncated": False})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    def search_text(self, query: str, path: str = ".") -> ToolResult:
        if not query.strip():
            return ToolResult(False, "empty_query", "", {})
        try:
            root = self.workspace.resolve(path)
            matches: list[dict[str, Any]] = []
            for candidate in root.rglob("*"):
                if any(part in SKIP_NAMES for part in candidate.parts) or not candidate.is_file() or candidate.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    for line_number, line in enumerate(candidate.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if query.lower() in line.lower():
                            matches.append({"path": str(candidate.relative_to(self.workspace.root)), "line": line_number, "text": line[:300]})
                            if len(matches) >= 80:
                                return ToolResult(True, "ok", "", {"matches": matches, "truncated": True})
                except OSError:
                    continue
            return ToolResult(True, "ok", "", {"matches": matches, "truncated": False})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    def write_file(self, path: str, content: str) -> ToolResult:
        try:
            target = self.workspace.resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            previous = target.read_text(encoding="utf-8", errors="replace") if existed else ""
            checkpoint_id = self.checkpoint.capture_before(path, target) if self.checkpoint else ""
            target.write_text(content, encoding="utf-8")
            if self.checkpoint:
                self.checkpoint.record_after(path, content)
            diff = "\n".join(difflib.unified_diff(previous.splitlines(), content.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))[:MAX_OUTPUT]
            return ToolResult(True, "ok", "", {"path": str(target.relative_to(self.workspace.root)), "created": not existed, "previous_bytes": len(previous.encode()), "new_bytes": len(content.encode()), "previous_sha256": content_sha256(previous), "sha256": content_sha256(content), "checkpoint_id": checkpoint_id, "diff": diff})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    def run_command(self, command: str, cwd: str = ".") -> ToolResult:
        if any(term in command.lower() for term in RISKY_COMMAND_TERMS):
            return ToolResult(False, "command_requires_approval", "", {"command": command})
        if any(term in command.lower() for term in BACKGROUND_COMMAND_TERMS):
            return ToolResult(False, "background_command_not_allowed", "", {"command": command})
        try:
            directory = self.workspace.resolve(cwd)
            if not directory.is_dir():
                return ToolResult(False, "not_a_directory", "", {})
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=directory,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + self.command_timeout_seconds
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    self._terminate_process(process)
                    return ToolResult(False, "command_cancelled", "", {"command": command})
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    return ToolResult(False, "command_timeout", "", {"seconds": self.command_timeout_seconds})
                try:
                    stdout, stderr = process.communicate(timeout=min(.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            output = (stdout + stderr)[:MAX_OUTPUT]
            return ToolResult(process.returncode == 0, "ok" if process.returncode == 0 else "command_failed", output, {"exit_code": process.returncode, "truncated": len(stdout + stderr) > MAX_OUTPUT})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
