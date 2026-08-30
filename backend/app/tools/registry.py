from __future__ import annotations

import subprocess
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.runtime.workspace import Workspace, WorkspaceError

SKIP_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv"}
MAX_OUTPUT = 12_000
MAX_FILE_BYTES = 160_000
RISKY_COMMAND_TERMS = ("rm -rf", "del /", "format ", "shutdown", "restart-computer", "remove-item -recurse", "git reset --hard")


@dataclass
class ToolResult:
    ok: bool
    code: str
    content: str
    meta: dict[str, Any]


class ToolRegistry:
    def __init__(self, workspace: Workspace, command_timeout_seconds: int = 30) -> None:
        self.workspace = workspace
        self.command_timeout_seconds = command_timeout_seconds

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
            previous = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            target.write_text(content, encoding="utf-8")
            diff = "\n".join(difflib.unified_diff(previous.splitlines(), content.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))[:MAX_OUTPUT]
            return ToolResult(True, "ok", "", {"path": str(target.relative_to(self.workspace.root)), "created": not bool(previous), "previous_bytes": len(previous.encode()), "new_bytes": len(content.encode()), "diff": diff})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})

    def run_command(self, command: str, cwd: str = ".") -> ToolResult:
        if any(term in command.lower() for term in RISKY_COMMAND_TERMS):
            return ToolResult(False, "command_requires_approval", "", {"command": command})
        try:
            directory = self.workspace.resolve(cwd)
            if not directory.is_dir():
                return ToolResult(False, "not_a_directory", "", {})
            completed = subprocess.run(command, shell=True, cwd=directory, text=True, capture_output=True, timeout=self.command_timeout_seconds, encoding="utf-8", errors="replace")
            output = (completed.stdout + completed.stderr)[:MAX_OUTPUT]
            return ToolResult(completed.returncode == 0, "ok" if completed.returncode == 0 else "command_failed", output, {"exit_code": completed.returncode, "truncated": len(completed.stdout + completed.stderr) > MAX_OUTPUT})
        except WorkspaceError as error:
            return ToolResult(False, str(error), "", {})
        except subprocess.TimeoutExpired:
            return ToolResult(False, "command_timeout", "", {"seconds": self.command_timeout_seconds})
