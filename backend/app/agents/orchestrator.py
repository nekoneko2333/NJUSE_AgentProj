from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.agents.roles import ROLE_POLICY
from app.core.models import AgentEvent, AgentRole, EventType
from app.core.settings import Settings
from app.llm.client import LLMError, OpenAICompatibleClient
from app.tools.registry import ToolRegistry, ToolResult

Publish = Callable[[AgentEvent], Awaitable[None]]

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {"type": "function", "function": {"name": "list_files", "description": "列出工作区某目录的直接子项。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    "read_file": {"type": "function", "function": {"name": "read_file", "description": "读取 UTF-8 文本文件的指定行。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["path"]}}},
    "search_text": {"type": "function", "function": {"name": "search_text", "description": "在工作区中检索文本。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}}},
    "write_file": {"type": "function", "function": {"name": "write_file", "description": "写入或创建工作区内的文本文件。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    "run_command": {"type": "function", "function": {"name": "run_command", "description": "在工作区内运行测试或开发命令。禁止危险或破坏性命令。", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command"]}}},
}


class Orchestrator:
    def __init__(self, session_id: str, task: str, tools: ToolRegistry, client: OpenAICompatibleClient, settings: Settings, publish: Publish) -> None:
        self.session_id, self.task, self.tools, self.client, self.settings, self.publish = session_id, task, tools, client, settings, publish
        self.history: list[dict[str, Any]] = []

    async def run(self) -> None:
        for role in (AgentRole.PLANNER, AgentRole.EXPLORER, AgentRole.CODER, AgentRole.REVIEWER):
            await self._run_role(role)
        await self.publish(AgentEvent(type=EventType.TASK_FINISHED, session_id=self.session_id, summary="四个角色均已完成；请查看变更和测试结果。"))

    async def _run_role(self, role: AgentRole) -> None:
        policy = ROLE_POLICY[role]
        await self.publish(AgentEvent(type=EventType.AGENT_STARTED, session_id=self.session_id, role=role, summary=policy["goal"]))
        system = {"role": "system", "content": f"你是 MossCode 的 {role.value}。{policy['goal']} 用户任务：{self.task}。只调用授权工具；完成后用中文简短总结。"}
        messages: list[dict[str, Any]] = [system, *self.history]
        permitted = [TOOL_SCHEMAS[name] for name in policy["tools"]]
        for _ in range(self.settings.max_turns // 3):
            try:
                assistant = await self.client.complete(messages, permitted)
            except LLMError as error:
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, role=role, summary=str(error)))
                raise
            tool_calls = assistant.get("tool_calls") or []
            messages.append(assistant)
            if not tool_calls:
                summary = (assistant.get("content") or "角色未给出文字总结").strip()[:800]
                self.history.extend(messages[1:])
                await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary))
                return
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError:
                    result = ToolResult(False, "invalid_tool_arguments", "", {})
                else:
                    await self.publish(AgentEvent(type=EventType.TOOL_REQUESTED, session_id=self.session_id, role=role, summary=f"请求工具：{name}", payload={"tool": name, "arguments": arguments}))
                    result = self._execute(role, name, arguments)
                payload = {"ok": result.ok, "code": result.code, "content": result.content, "meta": result.meta}
                await self.publish(AgentEvent(type=EventType.TOOL_FINISHED, session_id=self.session_id, role=role, summary=f"工具完成：{name}（{result.code}）", payload={"tool": name, "result": payload}))
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(payload, ensure_ascii=False)})
        await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, role=role, summary="agent_turn_limit_reached"))
        raise LLMError("agent_turn_limit_reached")

    def _execute(self, role: AgentRole, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in ROLE_POLICY[role]["tools"] or not hasattr(self.tools, name):
            return ToolResult(False, "tool_not_permitted", "", {})
        try:
            return getattr(self.tools, name)(**arguments)
        except (TypeError, ValueError):
            return ToolResult(False, "invalid_tool_arguments", "", {})
