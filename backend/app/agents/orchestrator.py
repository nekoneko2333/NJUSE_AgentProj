from __future__ import annotations

import json
import sys
import asyncio
import re
from typing import Any, Awaitable, Callable

from app.agents.roles import ROLE_POLICY
from app.core.context import ContextManager
from app.core.models import AgentEvent, AgentRole, EventType
from app.core.settings import Settings
from app.llm.client import LLMError, OpenAICompatibleClient
from app.tools.registry import ToolRegistry, ToolResult

Publish = Callable[[AgentEvent], Awaitable[None]]
CommandApproval = Callable[[AgentRole, str, str], Awaitable[bool]]

CHANGE_INTENT_WORDS = (
    "创建", "新增", "修改", "修复", "实现", "编写", "撰写", "制作", "搭建", "开发", "生成", "完成", "更新", "重构", "删除",
    "create", "add", "modify", "fix", "implement", "write", "build", "develop", "generate", "complete", "update", "refactor", "delete",
)
NO_CHANGE_PHRASES = (
    "只读", "不要修改", "不修改", "无需修改", "不要改动", "无需改动",
    "read-only", "read only", "do not modify", "without changes", "no changes",
)

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {"type": "function", "function": {"name": "list_files", "description": "列出工作区某目录的直接子项。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    "read_file": {"type": "function", "function": {"name": "read_file", "description": "读取 UTF-8 文本文件的指定行。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["path"]}}},
    "search_text": {"type": "function", "function": {"name": "search_text", "description": "在工作区中检索文本。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}}},
    "write_file": {"type": "function", "function": {"name": "write_file", "description": "写入或创建工作区内的文本文件。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    "run_command": {"type": "function", "function": {"name": "run_command", "description": "在工作区内运行测试或开发命令。禁止危险或破坏性命令。", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command"]}}},
}
ROLE_TURN_LIMITS = {
    AgentRole.PLANNER: 4,
    AgentRole.EXPLORER: 6,
    AgentRole.CODER: 10,
    AgentRole.REVIEWER: 8,
}


def parse_reviewer_verdict(summary: str) -> tuple[bool, str]:
    """解析模型可能放在段首或段中的最后一个规范验收标记。"""
    matches = list(re.finditer(r"VERDICT:\s*(PASS|NEEDS_WORK)", summary, flags=re.IGNORECASE))
    if not matches:
        return False, summary.strip()
    passed = matches[-1].group(1).upper() == "PASS"
    detail = re.sub(r"VERDICT:\s*(?:PASS|NEEDS_WORK)", "", summary, flags=re.IGNORECASE).strip(" :\n")
    return passed, detail


class Orchestrator:
    def __init__(self, session_id: str, task: str, tools: ToolRegistry, client: OpenAICompatibleClient, settings: Settings, publish: Publish, locale: str = "zh-CN", conversation_context: str = "", context_manager: ContextManager | None = None, command_mode: str = "auto", request_command_approval: CommandApproval | None = None, cancelled: Callable[[], bool] | None = None, project_rules: str = "") -> None:
        self.session_id, self.task, self.tools, self.client, self.settings, self.publish = session_id, task, tools, client, settings, publish
        self.handoffs: list[str] = []
        self.locale = locale
        self.conversation_context = conversation_context or "这是该会话的第一轮任务。"
        self.context_manager = context_manager or ContextManager(budget_chars=settings.context_budget_chars, recent_turns=settings.context_recent_turns, summary_chars=settings.context_summary_chars)
        self.changed_files: set[str] = set()
        self.role_summaries: dict[AgentRole, str] = {}
        self.failure_reason = ""
        self.review_detail = ""
        self.completion_source = "reviewer"
        self.execution_evidence: list[tuple[str, bool]] = []
        self.command_mode = command_mode
        self.request_command_approval = request_command_approval
        self.cancelled = cancelled or (lambda: False)
        self.project_rules = project_rules
        normalized_task = task.lower()
        self.requires_change = not any(phrase in normalized_task for phrase in NO_CHANGE_PHRASES) and any(word in normalized_task for word in CHANGE_INTENT_WORDS)

    async def run(self) -> bool:
        for role in (AgentRole.PLANNER, AgentRole.EXPLORER, AgentRole.CODER, AgentRole.REVIEWER):
            if self.cancelled():
                raise asyncio.CancelledError
            role_finished = await self._run_role(role)
            if role is AgentRole.CODER and self.requires_change and not self.changed_files:
                self.failure_reason = "required_change_not_written"
                await self.publish(AgentEvent(
                    type=EventType.TASK_FAILED,
                    session_id=self.session_id,
                    role=role,
                    summary="任务要求修改文件，但实现者没有产生任何写入。已停止审查，请查看实现者说明。",
                    payload={"reason": "required_change_not_written", "changed_files": []},
                ))
                return False
            if role is AgentRole.REVIEWER and not role_finished:
                if self._has_structured_completion_evidence():
                    self.completion_source = "structured_evidence"
                    self.review_detail = "文件修改后已有成功验证，系统已根据结构化工具证据完成收尾。"
                    continue
                self.failure_reason = "reviewer_incomplete"
                await self.publish(AgentEvent(
                    type=EventType.TASK_FAILED,
                    session_id=self.session_id,
                    role=role,
                    summary="审查阶段达到轮次上限，且缺少写入后的成功验证。已保留改动，但不会将本轮标记为完成。",
                    payload={"reason": self.failure_reason, "changed_files": sorted(self.changed_files)},
                ))
                return False
            if role is AgentRole.REVIEWER:
                review = self.role_summaries.get(role, "")
                review_passed, self.review_detail = parse_reviewer_verdict(review)
                if not review_passed:
                    explicit_verdict = re.search(r"VERDICT:\s*(PASS|NEEDS_WORK)", review, flags=re.IGNORECASE)
                    if explicit_verdict is None and self._has_structured_completion_evidence():
                        self.completion_source = "structured_evidence"
                        self.review_detail = "文件修改后已有成功验证，系统已根据结构化工具证据完成收尾。"
                        continue
                    self.failure_reason = "reviewer_rejected"
                    detail = self.review_detail or "审查未通过，仍有验收项需要处理。"
                    await self.publish(AgentEvent(
                        type=EventType.TASK_FAILED,
                        session_id=self.session_id,
                        role=role,
                        summary=detail,
                        payload={"reason": self.failure_reason, "content": detail, "changed_files": sorted(self.changed_files)},
                    ))
                    return False
        hook_result = await self._run_hooks("before_finish", AgentRole.REVIEWER)
        if hook_result is not None and not hook_result.ok:
            self.failure_reason = "before_finish_hook_failed"
            await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, summary="结束前 Hook 未通过，改动已保留。", payload={"reason": self.failure_reason, "content": hook_result.content}))
            return False
        if self.changed_files:
            file_list = "\n".join(f"- `{path}`" for path in sorted(self.changed_files))
            review = self.review_detail or "修改已经完成。"
            summary = f"已完成本轮任务，共修改 {len(self.changed_files)} 个文件。\n\n{review}\n\n变更文件：\n{file_list}"
        elif not self.requires_change:
            result = self.review_detail or "检查已经完成。"
            summary = f"只读检查已完成，本次未修改文件。\n\n{result}"
        else:
            summary = "四个角色均已完成，但本次没有写入文件；请检查实现者的说明和工具记录。"
        await self.publish(AgentEvent(
            type=EventType.TASK_FINISHED,
            session_id=self.session_id,
            summary=summary,
            payload={"changed_files": sorted(self.changed_files), "completion_source": self.completion_source, "verification_status": "passed"},
        ))
        return True

    def _has_structured_completion_evidence(self) -> bool:
        """Reviewer 未形成结论时，仅用“最后写入后的最终验证成功”兜底。"""
        successful_writes = [index for index, (name, ok) in enumerate(self.execution_evidence) if name == "write_file" and ok]
        if not successful_writes:
            return False
        validations = [(index, ok) for index, (name, ok) in enumerate(self.execution_evidence) if name == "run_command" and index > successful_writes[-1]]
        return bool(validations) and validations[-1][1]

    async def _run_role(self, role: AgentRole) -> bool:
        policy = ROLE_POLICY[role]
        start_summary = "核对文件状态并遵循只读要求。" if role is AgentRole.CODER and not self.requires_change else policy["goal"]
        await self.publish(AgentEvent(type=EventType.AGENT_STARTED, session_id=self.session_id, role=role, summary=start_summary))
        language = "English" if self.locale == "en-US" else "Chinese"
        implementation_contract = ""
        if role is AgentRole.CODER:
            implementation_contract = (
                " If the user requests a code or file change, you MUST inspect the relevant files, "
                "call write_file for the implementation, and call run_command for an appropriate verification. "
                "Never claim an implementation is complete without a successful write_file result. "
                "You may use at most 5 list/read/search calls before your first write_file; use the explorer handoff and then implement instead of repeatedly inspecting."
            )
        runtime_contract = (
            f" The backend is running on Windows. When verifying Python code, use the exact interpreter "
            f"{json.dumps(sys.executable)} instead of guessing python, python3, or py. "
            "Validate JavaScript with Node.js or a browser; never pass JavaScript source to Python compile()."
            if "run_command" in policy["tools"] else ""
        )
        review_contract = (
            " Your final answer MUST start with exactly `VERDICT: PASS` only when the user requirements are verified by file inspection and relevant successful tests; otherwise start with exactly `VERDICT: NEEDS_WORK` and state the remaining failures."
            if role is AgentRole.REVIEWER else ""
        )
        system = {"role": "system", "content": f"You are MossCode's {role.value} agent. {policy['goal']} Only call permitted tools.{implementation_contract}{runtime_contract}{review_contract} Your final answer must be natural, direct, under 180 Chinese characters or 120 English words, and must not repeat earlier agents' plans. Reply in {language}."}
        handoff_text = "\n".join(self.handoffs[-3:]) or "No prior agent handoff."
        rules_text = self.project_rules or "No project-specific rules were found."
        messages: list[dict[str, Any]] = [system, {"role": "user", "content": f"Conversation memory (reference only):\n{self.conversation_context}\n\nProject rules (read-only, highest local priority):\n{rules_text}\n\nCurrent user task: {self.task}\nPrior agent handoffs in this turn (reference only; do your own role):\n{handoff_text}"}]
        permitted = [TOOL_SCHEMAS[name] for name in policy["tools"]]
        if hasattr(self.tools, "mcp_schemas"):
            permitted.extend(await asyncio.to_thread(self.tools.mcp_schemas, role))
        repeated_calls: dict[str, int] = {}
        implementation_reminded = False
        successful_tools: list[str] = []
        coder_inspection_calls = 0
        for _ in range(min(self.settings.max_turns, ROLE_TURN_LIMITS[role])):
            if self.cancelled():
                raise asyncio.CancelledError
            try:
                assistant = await self.client.complete(self.context_manager.trim_role_messages(messages), permitted)
            except LLMError as error:
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, role=role, summary=str(error)))
                raise
            tool_calls = assistant.get("tool_calls") or []
            messages.append(assistant)
            if not tool_calls:
                summary = (assistant.get("content") or "角色未给出文字总结").strip()[:800]
                if role is AgentRole.CODER and self.requires_change and not self.changed_files and not implementation_reminded:
                    implementation_reminded = True
                    messages.append({"role": "user", "content": "The requested change is not implemented yet. You have write_file and run_command. Call write_file now, then verify the result. Do not repeat the plan."})
                    continue
                self.handoffs.append(f"{role.value}: {summary}")
                self.role_summaries[role] = summary
                await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary}))
                return True
            for call in tool_calls:
                if self.cancelled():
                    raise asyncio.CancelledError
                name = call.get("function", {}).get("name", "")
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError:
                    result = ToolResult(False, "invalid_tool_arguments", "", {})
                else:
                    signature = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
                    repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
                    if repeated_calls[signature] > 2:
                        result = ToolResult(False, "repeated_tool_call", "", {"tool": name})
                    elif role is AgentRole.CODER and not self.changed_files and name in {"list_files", "read_file", "search_text"} and coder_inspection_calls >= 5:
                        result = ToolResult(False, "inspection_budget_exhausted", "Call write_file now using the context already collected.", {"limit": 5})
                    else:
                        await self.publish(AgentEvent(type=EventType.TOOL_REQUESTED, session_id=self.session_id, role=role, summary=f"请求工具：{name}", payload={"tool": name, "arguments": arguments}))
                        result = await self._execute(role, name, arguments)
                        if role is AgentRole.CODER and name in {"list_files", "read_file", "search_text"}:
                            coder_inspection_calls += 1
                payload = {"ok": result.ok, "code": result.code, "content": result.content, "meta": result.meta}
                if result.ok:
                    successful_tools.append(name)
                await self.publish(AgentEvent(type=EventType.TOOL_FINISHED, session_id=self.session_id, role=role, summary=f"工具完成：{name}（{result.code}）", payload={"tool": name, "arguments": arguments, "result": payload}))
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(payload, ensure_ascii=False)})
        if role is AgentRole.REVIEWER:
            try:
                forced = await self.client.complete(
                    self.context_manager.trim_role_messages(messages + [{"role": "user", "content": "Stop using tools. Based only on the evidence already collected, return the required VERDICT marker and a concise final review now."}]),
                    [],
                )
                if not forced.get("tool_calls") and (forced.get("content") or "").strip():
                    summary = str(forced["content"]).strip()[:800]
                    self.handoffs.append(f"{role.value}: {summary}")
                    self.role_summaries[role] = summary
                    await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary, "reason": "forced_summary"}))
                    return True
            except LLMError:
                pass
        if self.locale == "en-US":
            summary = "This role reached its turn limit after completing tool operations; review the tool records for the retained results." if successful_tools else "This role reached its turn limit before producing a final summary."
        else:
            summary = "本角色已完成工具操作，但在整理总结前达到轮次上限；结果已保留在工具记录中。" if successful_tools else "本角色在给出最终总结前达到轮次上限，请检查工具记录。"
        self.handoffs.append(f"{role.value}: {summary}")
        self.role_summaries[role] = summary
        await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary, "reason": "turn_limit"}))
        return False

    async def _execute(self, role: AgentRole, name: str, arguments: dict[str, Any]) -> ToolResult:
        is_mcp = "/" in name
        if not is_mcp and (name not in ROLE_POLICY[role]["tools"] or not hasattr(self.tools, name)):
            return ToolResult(False, "tool_not_permitted", "", {})
        before = await self._run_hooks("before_tool", role)
        if before is not None and not before.ok:
            return before
        if is_mcp and self.tools.mcp.is_write_tool(name):
            if self.request_command_approval is None:
                return ToolResult(False, "mcp_write_approval_unavailable", "", {})
            if not await self.request_command_approval(role, f"MCP {name}", "."):
                return ToolResult(False, "mcp_write_permission_denied", "", {"tool": name})
        if name == "run_command":
            if self.command_mode == "deny":
                return ToolResult(False, "command_permission_denied", "", {"command": arguments.get("command", "")})
            if self.command_mode == "ask":
                if self.request_command_approval is None:
                    return ToolResult(False, "command_approval_unavailable", "", {})
                allowed = await self.request_command_approval(role, str(arguments.get("command", "")), str(arguments.get("cwd", ".")))
                if not allowed:
                    return ToolResult(False, "command_permission_denied", "", {"command": arguments.get("command", "")})
        try:
            result = await asyncio.to_thread(self.tools.call_mcp, role, name, arguments) if is_mcp else await asyncio.to_thread(getattr(self.tools, name), **arguments)
            if name in {"write_file", "run_command"}:
                self.execution_evidence.append((name, result.ok))
            if name == "write_file" and result.ok and result.meta.get("path"):
                self.changed_files.add(str(result.meta["path"]))
            after = await self._run_hooks("after_tool", role)
            if after is not None and not after.ok:
                return after
            if name == "write_file" and result.ok:
                after_write = await self._run_hooks("after_write", role)
                if after_write is not None and not after_write.ok:
                    return after_write
            return result
        except (TypeError, ValueError):
            return ToolResult(False, "invalid_tool_arguments", "", {})

    async def _run_hooks(self, event: str, role: AgentRole) -> ToolResult | None:
        commands = self.tools.hook_commands(event) if hasattr(self.tools, "hook_commands") else []
        if not commands:
            return None
        for command in commands:
            if self.cancelled():
                raise asyncio.CancelledError
            if self.command_mode == "deny":
                return ToolResult(False, "hook_command_permission_denied", "", {"event": event})
            if self.request_command_approval is None:
                return ToolResult(False, "hook_approval_unavailable", "", {"event": event})
            if not await self.request_command_approval(role, command, "."):
                return ToolResult(False, "hook_command_permission_denied", "", {"event": event})
            result = await asyncio.to_thread(self.tools.run_command, command, ".")
            if not result.ok:
                return ToolResult(False, "hook_failed", result.content, {"event": event, "command": command})
        return ToolResult(True, "ok", "", {"event": event, "count": len(commands)})
