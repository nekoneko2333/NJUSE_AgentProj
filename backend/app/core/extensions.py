from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.models import AgentRole


RULE_LIMIT_CHARS = 24_000
HOOK_EVENTS = {"before_tool", "after_tool", "after_write", "before_finish"}


@dataclass(frozen=True)
class ProjectRules:
    text: str
    sources: list[dict[str, object]]
    truncated: bool


def load_project_rules(workspace: Path, limit: int = RULE_LIMIT_CHARS) -> ProjectRules:
    candidates = [workspace / "AGENTS.md"]
    rules_dir = workspace / ".mosscode" / "rules"
    if rules_dir.is_dir():
        candidates.extend(sorted(rules_dir.glob("*.md"), key=lambda item: item.name.casefold()))
    chunks: list[str] = []
    sources: list[dict[str, object]] = []
    used = 0
    truncated = False
    for candidate in candidates:
        if not candidate.is_file():
            continue
        content = candidate.read_text(encoding="utf-8", errors="replace")
        remaining = max(0, limit - used)
        included = content[:remaining]
        relative = candidate.relative_to(workspace).as_posix()
        sources.append({"path": relative, "chars": len(included), "truncated": len(included) < len(content)})
        if included:
            chunks.append(f"## {relative}\n{included}")
        used += len(included)
        if len(included) < len(content) or used >= limit:
            truncated = True
            break
    return ProjectRules("\n\n".join(chunks), sources, truncated)


class HookConfig:
    def __init__(self, workspace: Path) -> None:
        self.path = workspace / ".mosscode" / "hooks.json"
        self.enabled = False
        self.events: dict[str, list[str]] = {}
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.enabled = payload.get("enabled") is True
            hooks = payload.get("hooks", {})
            if isinstance(hooks, dict):
                for event, commands in hooks.items():
                    if event in HOOK_EVENTS and isinstance(commands, list):
                        self.events[event] = [str(command) for command in commands if str(command).strip()]
        except (OSError, json.JSONDecodeError):
            self.enabled = False

    def commands(self, event: str) -> list[str]:
        return list(self.events.get(event, [])) if self.enabled else []

    def status(self) -> dict[str, object]:
        return {"configured": self.path.is_file(), "enabled": self.enabled, "events": {name: len(commands) for name, commands in self.events.items()}}


class MCPManager:
    """Small local stdio MCP client. Each discovery/call gets an isolated process."""

    def __init__(self, workspace: Path, timeout_seconds: int = 15) -> None:
        self.workspace = workspace
        self.path = workspace / ".mosscode" / "mcp.json"
        self.timeout_seconds = timeout_seconds
        self._config: dict[str, Any] = {}
        self._tools: dict[str, dict[str, Any]] | None = None
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload.get("servers"), dict):
                    self._config = payload["servers"]
            except (OSError, json.JSONDecodeError):
                self._config = {}

    def _request(self, server_name: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        server = self._config.get(server_name, {})
        command = server.get("command")
        args = server.get("args", [])
        if server.get("enabled", True) is not True or not isinstance(command, str) or not isinstance(args, list):
            raise RuntimeError("mcp_server_disabled")
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "MossCode", "version": "0.1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
        ]
        input_text = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
        try:
            completed = subprocess.run(
                [command, *[str(value) for value in args]],
                input=input_text,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("mcp_transport_failed") from error
        for line in completed.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError("mcp_tool_failed")
                return message.get("result", {})
        raise RuntimeError("mcp_invalid_response")

    def discover(self) -> dict[str, dict[str, Any]]:
        if self._tools is not None:
            return self._tools
        discovered: dict[str, dict[str, Any]] = {}
        for server_name in sorted(self._config):
            server = self._config[server_name]
            if not isinstance(server, dict) or server.get("enabled", True) is not True:
                continue
            try:
                result = self._request(server_name, "tools/list")
            except RuntimeError:
                continue
            policies = server.get("tools", {}) if isinstance(server.get("tools"), dict) else {}
            for tool in result.get("tools", []):
                name = tool.get("name")
                if not isinstance(name, str):
                    continue
                policy = policies.get(name, {}) if isinstance(policies.get(name), dict) else {}
                discovered[f"{server_name}/{name}"] = {
                    "server": server_name,
                    "name": name,
                    "description": str(tool.get("description", "Local MCP tool")),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                    "roles": policy.get("roles", ["coder"]),
                    "write": policy.get("write") is True,
                }
        self._tools = discovered
        return discovered

    def schemas_for(self, role: AgentRole) -> list[dict[str, Any]]:
        role_names = {role.value, "coder"} if role is AgentRole.SINGLE else {role.value}
        return [{"type": "function", "function": {"name": qualified, "description": tool["description"], "parameters": tool["inputSchema"]}} for qualified, tool in self.discover().items() if role_names.intersection(tool["roles"])]

    def call(self, qualified_name: str, arguments: dict[str, Any], role: AgentRole) -> tuple[bool, str, dict[str, Any]]:
        tool = self.discover().get(qualified_name)
        role_names = {role.value, "coder"} if role is AgentRole.SINGLE else {role.value}
        if tool is None or not role_names.intersection(tool["roles"]):
            return False, "mcp_tool_not_permitted", {}
        try:
            result = self._request(tool["server"], "tools/call", {"name": tool["name"], "arguments": arguments})
        except RuntimeError as error:
            return False, str(error), {}
        failed = result.get("isError") is True
        text = "\n".join(str(item.get("text", "")) for item in result.get("content", []) if isinstance(item, dict) and item.get("type") == "text")
        return not failed, text, {"mcp": qualified_name, "write": tool["write"], "raw": result}

    def is_write_tool(self, qualified_name: str) -> bool:
        tool = self.discover().get(qualified_name)
        return bool(tool and tool["write"])

    def status(self) -> dict[str, object]:
        return {"configured": self.path.is_file(), "servers": sorted(self._config), "tools": sorted(self.discover()) if self._config else []}
