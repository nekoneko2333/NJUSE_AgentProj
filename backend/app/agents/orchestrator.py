from __future__ import annotations

import json
import sys
import asyncio
import os
import re
from typing import Any, Awaitable, Callable

from app.agents.roles import ROLE_POLICY
from app.core.context import ContextManager
from app.core.models import AgentEvent, AgentRole, EventType
from app.core.settings import Settings
from app.llm.client import LLMError, OpenAICompatibleClient
from app.runtime.workspace import WorkspaceError
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
ENGINEERING_INTENT_WORDS = (
    "文件", "代码", "项目", "仓库", "接口", "测试", "错误", "bug", "页面", "前端", "后端", "数据库", "终端", "命令", "检查", "分析", "解释代码",
    "file", "code", "project", "repository", "repo", "api", "test", "error", "page", "frontend", "backend", "database", "terminal", "command", "debug",
)
CONTINUATION_PHRASES = (
    "重试", "再试一次", "重新试", "继续", "继续做", "继续完成", "接着做", "接着完成", "完成上面的", "完成之前的",
    "retry", "try again", "continue", "keep going", "resume",
)

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {"type": "function", "function": {"name": "list_files", "description": "列出工作区某目录的直接子项。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    "read_file": {"type": "function", "function": {"name": "read_file", "description": "读取 UTF-8 文本文件的指定行。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["path"]}}},
    "search_text": {"type": "function", "function": {"name": "search_text", "description": "在工作区中检索文本。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}}},
    "write_file": {"type": "function", "function": {"name": "write_file", "description": "写入或创建工作区内的文本文件。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    "run_command": {"type": "function", "function": {"name": "run_command", "description": "在工作区内运行测试或开发命令。禁止危险或破坏性命令。", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["command"]}}},
}
ROLE_TURN_LIMITS = {
    AgentRole.SINGLE: 12,
    AgentRole.PLANNER: 4,
    AgentRole.EXPLORER: 6,
    AgentRole.CODER: 10,
    AgentRole.REVIEWER: 8,
}
MAX_REPAIR_CYCLES = 5
INSPECTION_TOOLS = {"list_files", "read_file", "search_text"}
REVIEW_BASES = {"USER_REQUIREMENT", "FAILED_VERIFICATION", "REGRESSION"}
ORCHESTRATOR_PROTOCOL = "root-owned-review-v2"

DEFAULT_AGENT_CONFIG: dict[str, dict[str, Any]] = {
    role.value: {"enabled": True, "max_turns": limit, "instruction": ""}
    for role, limit in ROLE_TURN_LIMITS.items()
}


def normalize_agent_config(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Clamp user-editable workflow settings before they reach prompts or loops."""
    source = config if isinstance(config, dict) else {}
    normalized: dict[str, dict[str, Any]] = {}
    for role, defaults in DEFAULT_AGENT_CONFIG.items():
        value = source.get(role, {})
        value = value if isinstance(value, dict) else {}
        instruction = str(value.get("instruction", "")).strip()[:2000]
        try:
            max_turns = int(value.get("max_turns", defaults["max_turns"]))
        except (TypeError, ValueError):
            max_turns = int(defaults["max_turns"])
        normalized[role] = {
            "enabled": bool(value.get("enabled", defaults["enabled"])),
            "max_turns": max(1, min(30, max_turns)),
            "instruction": instruction,
        }
    normalized[AgentRole.CODER.value]["enabled"] = True
    normalized[AgentRole.SINGLE.value]["enabled"] = True
    return normalized


def resolve_agent_mode(task: str, requested_mode: str) -> str:
    if requested_mode != "adaptive":
        return "single" if requested_mode == "single" else "multi"
    normalized = task.lower()
    multi_signals = ("多个", "全栈", "架构", "重构", "迁移", "测试", "评审", "规划", "multi", "architecture", "refactor", "migrate")
    return "multi" if len(task) >= 180 or any(signal in normalized for signal in multi_signals) else "single"


def is_conversational_task(task: str) -> bool:
    """Route ordinary chat and preference questions away from code-review semantics."""
    normalized = task.lower()
    if any(word in normalized for word in CHANGE_INTENT_WORDS):
        return False
    return not any(word in normalized for word in ENGINEERING_INTENT_WORDS)


def is_continuation_task(task: str) -> bool:
    """识别必须结合上一项实质任务理解的短指令，避免把“重试”当成孤立闲聊。"""
    normalized = " ".join(task.lower().strip(" \t\r\n。！？!?，,；;：:").split())
    if not normalized or len(normalized) > 100:
        return False
    return any(normalized == phrase or normalized.startswith(phrase) for phrase in CONTINUATION_PHRASES)


def is_inspection_command(command: str) -> bool:
    """识别借 run_command 查看文件的调用，使其与 read/list/search 共用探查预算。"""
    normalized = " ".join(command.strip().lower().split())
    direct = r"^(?:dir|tree|type|cat|more|ls|rg|grep|findstr|get-childitem|get-content|select-string)\b"
    return bool(re.search(direct, normalized) or "print(open(" in normalized or ".read_text(" in normalized or ".read_bytes(" in normalized)


def is_file_mutation_command(command: str) -> bool:
    """命令工具只负责验证；显式文件修改必须走 write_file，以保留 Diff、检查点和审计。"""
    normalized = " ".join(command.strip().lower().split())
    mutation_patterns = (
        r"\b(?:set-content|add-content|out-file|new-item|remove-item|copy-item|move-item|rename-item)\b",
        r"(?:^|[;&|])\s*(?:copy|move|ren|del|mkdir|md|touch|rm)\b",
        r"\bsed\s+-i\b",
        r"\.write_text\s*\(",
        r"\.write_bytes\s*\(",
        r"\.write\s*\(",
        r"open\s*\([^)]*,\s*['\"][wax+]",
    )
    return any(re.search(pattern, normalized) for pattern in mutation_patterns)


def parse_reviewer_verdict(summary: str) -> tuple[bool, str]:
    """解析模型可能放在段首或段中的最后一个规范验收标记。"""
    matches = list(re.finditer(r"VERDICT:\s*(PASS|NEEDS_WORK)", summary, flags=re.IGNORECASE))
    if not matches:
        return False, summary.strip()
    passed = matches[-1].group(1).upper() == "PASS"
    detail = re.sub(r"VERDICT:\s*(?:PASS|NEEDS_WORK)", "", summary, flags=re.IGNORECASE).strip(" :\n")
    return passed, detail


def parse_reviewer_basis(summary: str) -> str:
    """拒绝结论必须说明依据；没有依据的自由发挥不能成为终止任务的权限。"""
    match = re.search(r"(?:^|\n)BASIS:\s*([A-Z_]+)", summary, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


class Orchestrator:
    def __init__(self, session_id: str, task: str, tools: ToolRegistry, client: OpenAICompatibleClient, settings: Settings, publish: Publish, locale: str = "zh-CN", conversation_context: str = "", context_manager: ContextManager | None = None, command_mode: str = "auto", request_command_approval: CommandApproval | None = None, cancelled: Callable[[], bool] | None = None, project_rules: str = "", execution_mode: str = "multi", agent_config: dict[str, Any] | None = None, memory_metadata: dict[str, Any] | None = None, reference_task: str = "", resume_existing: bool = False) -> None:
        self.session_id, self.task, self.tools, self.client, self.settings, self.publish = session_id, task, tools, client, settings, publish
        self.handoffs: list[str] = []
        self.locale = locale
        self.conversation_context = conversation_context or "这是该会话的第一轮任务。"
        self.context_manager = context_manager or ContextManager(budget_chars=settings.context_budget_chars, recent_turns=settings.context_recent_turns, summary_chars=settings.context_summary_chars)
        self.changed_files: set[str] = set()
        self.role_summaries: dict[AgentRole, str] = {}
        self.failure_reason = ""
        self.review_detail = ""
        self.open_review_issue = ""
        self.completion_source = "reviewer"
        self.execution_evidence: list[tuple[str, bool]] = []
        self.evidence_details: list[dict[str, Any]] = []
        self.repair_cycles = 0
        self.command_mode = command_mode
        self.request_command_approval = request_command_approval
        self.cancelled = cancelled or (lambda: False)
        self.project_rules = project_rules
        self.reference_task = reference_task.strip()
        self.is_continuation = is_continuation_task(task) and bool(self.reference_task)
        self.effective_task = f"{self.reference_task}\n后续指令：{task}" if self.is_continuation else task
        # 验收范围只来自用户当前任务（含明确续跑引用），不会被 Planner/Explorer/Reviewer 扩张。
        self.acceptance_contract = self.effective_task
        self.resume_existing = bool(resume_existing or self.is_continuation)
        self.conversation_only = is_conversational_task(self.effective_task)
        self.execution_mode = "single" if self.conversation_only else resolve_agent_mode(self.effective_task, execution_mode)
        self.agent_config = normalize_agent_config(agent_config)
        self.memory_metadata = memory_metadata or {}
        normalized_task = self.effective_task.lower()
        self.requires_change = not any(phrase in normalized_task for phrase in NO_CHANGE_PHRASES) and any(word in normalized_task for word in CHANGE_INTENT_WORDS)

    async def run(self) -> bool:
        if self.execution_mode == "single":
            roles = [AgentRole.SINGLE]
        else:
            roles = [role for role in (AgentRole.PLANNER, AgentRole.EXPLORER, AgentRole.CODER, AgentRole.REVIEWER) if self.agent_config[role.value]["enabled"]]
        has_reviewer = AgentRole.REVIEWER in roles
        implementers = {AgentRole.CODER, AgentRole.SINGLE}
        for role in (item for item in roles if item is not AgentRole.REVIEWER):
            if self.cancelled():
                raise asyncio.CancelledError
            role_finished = await self._run_role(role)
            if role in implementers and self.requires_change and not self.changed_files and not self.resume_existing:
                self.failure_reason = "required_change_not_written"
                await self.publish(AgentEvent(
                    type=EventType.TASK_FAILED,
                    session_id=self.session_id,
                    role=role,
                    summary="任务要求修改文件，但实现者没有产生任何写入。已停止审查，请查看实现者说明。",
                    payload={"reason": "required_change_not_written", "changed_files": []},
                ))
                return False
            if not role_finished and role in {AgentRole.PLANNER, AgentRole.EXPLORER}:
                # 规划和探索只提供上下文，不应因总结超时阻断真正的实现阶段。
                continue
            if not role_finished and role in implementers and not has_reviewer:
                self.failure_reason = f"{role.value}_incomplete"
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, role=role, summary="执行达到该角色轮次上限，尚未形成可验收结论。", payload={"reason": self.failure_reason, "changed_files": sorted(self.changed_files)}))
                return False
        if has_reviewer and not await self._review_with_repairs():
            return False
        if not has_reviewer:
            if self.requires_change and not self._has_structured_completion_evidence():
                self.failure_reason = "verification_incomplete"
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, summary="文件已修改，但缺少最后一次写入后的成功验证。改动已保留。", payload={"reason": self.failure_reason, "changed_files": sorted(self.changed_files), "execution_mode": self.execution_mode}))
                return False
            final_role = roles[-1]
            self.review_detail = self.role_summaries.get(final_role, "任务已完成。")
            self.completion_source = "structured_evidence" if self.requires_change else "agent_summary"
        finishing_role = AgentRole.REVIEWER if has_reviewer else roles[-1]
        hook_result = None if self.conversation_only else await self._run_hooks("before_finish", finishing_role)
        if hook_result is not None and not hook_result.ok:
            self.failure_reason = "before_finish_hook_failed"
            await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, summary="结束前 Hook 未通过，改动已保留。", payload={"reason": self.failure_reason, "content": hook_result.content}))
            return False
        if self.changed_files:
            file_list = "\n".join(f"- `{path}`" for path in sorted(self.changed_files))
            review = self.review_detail or "修改已经完成。"
            summary = f"已完成本轮任务，共修改 {len(self.changed_files)} 个文件。\n\n{review}\n\n变更文件：\n{file_list}"
        elif self.conversation_only:
            summary = self.review_detail or "已经完成回复。"
        elif not self.requires_change:
            result = self.review_detail or "检查已经完成。"
            summary = f"只读检查已完成，本次未修改文件。\n\n{result}"
        else:
            summary = "执行流程已经结束，但本次没有写入文件；请检查执行说明和工具记录。"
        await self.publish(AgentEvent(
            type=EventType.TASK_FINISHED,
            session_id=self.session_id,
            summary=summary,
            payload={"changed_files": sorted(self.changed_files), "completion_source": self.completion_source, "verification_status": "passed", "execution_mode": self.execution_mode, "intent": "conversation" if self.conversation_only else "engineering", "repair_cycles": self.repair_cycles},
        ))
        return True

    async def _review_with_repairs(self) -> bool:
        """Reviewer 仅提供受约束的诊断；主编排器持有修复循环与最终状态。"""
        while True:
            role_finished = await self._run_role(
                AgentRole.REVIEWER,
                phase_payload={"repair_cycle": self.repair_cycles},
            )
            review = self.role_summaries.get(AgentRole.REVIEWER, "")
            review_passed, self.review_detail = parse_reviewer_verdict(review)
            explicit_verdict = re.search(r"VERDICT:\s*(PASS|NEEDS_WORK)", review, flags=re.IGNORECASE)
            review_basis = parse_reviewer_basis(review)
            rejection_supported = bool(
                explicit_verdict
                and explicit_verdict.group(1).upper() == "NEEDS_WORK"
                and review_basis in REVIEW_BASES
                and re.search(r"(?:^|\n)EVIDENCE:\s*\S", review, flags=re.IGNORECASE)
                and re.search(r"(?:^|\n)ACTION:\s*\S", review, flags=re.IGNORECASE)
            )

            if role_finished and review_passed:
                if not self.requires_change or self._has_structured_completion_evidence():
                    self.completion_source = "reviewer"
                    return True
                review_passed = False
                self.review_detail = "审查给出了通过结论，但最后一次文件写入后缺少成功验证。"

            if explicit_verdict is not None and explicit_verdict.group(1).upper() == "NEEDS_WORK" and not rejection_supported:
                if self._has_structured_completion_evidence():
                    self.completion_source = "structured_evidence"
                    self.review_detail = "实现者已完成写入和成功验证；复核意见缺少可追溯的验收依据，未被提升为新的用户要求。"
                    return True
                self.review_detail = "复核未提供完整的 BASIS、EVIDENCE 和 ACTION；请依据最后一次失败验证修复，不扩张验收范围。"

            if not role_finished or explicit_verdict is None:
                if self._has_structured_completion_evidence():
                    self.completion_source = "structured_evidence"
                    self.review_detail = "审查总结未完整返回；文件修改后已有成功验证，系统已根据结构化工具证据完成收尾。"
                    return True
                if not self.review_detail:
                    self.review_detail = "审查未形成可验证的结论。"

            if self.requires_change and self.repair_cycles < MAX_REPAIR_CYCLES:
                self.repair_cycles += 1
                repair_request = self.review_detail or "重新核对用户要求、文件内容和验证结果，并补齐尚未完成的部分。"
                self.open_review_issue = repair_request
                await self._run_role(
                    AgentRole.CODER,
                    phase_instruction=(
                        f"这是第 {self.repair_cycles} 次内部有限返工。唯一待处理问题：{repair_request}\n"
                        "你是持续负责结果的主实现者。直接结合下方运行证据定位并修复；不要重新规划整个项目，也不要改写已经通过的部分。"
                        "修复后运行与上一轮相同或更完整的权威测试。"
                    ),
                    phase_payload={"repair_cycle": self.repair_cycles, "repairing": True},
                )
                continue

            latest_validation = next((item for item in reversed(self.evidence_details) if item["tool"] == "run_command"), None)
            if latest_validation and not latest_validation["ok"]:
                self.failure_reason = "verification_failed_after_repairs"
                detail = self.review_detail or "多次内部修复后，权威验证仍未通过。"
            elif not self._has_structured_completion_evidence():
                self.failure_reason = "verification_incomplete_after_repairs"
                detail = self.review_detail or "内部修复已耗尽，但还没有最后一次写入后的成功验证。"
            else:
                self.failure_reason = "acceptance_gap_after_repairs"
                detail = self.review_detail or "仍有一项可追溯的用户验收要求未满足。"
            await self.publish(AgentEvent(
                type=EventType.TASK_FAILED,
                session_id=self.session_id,
                role=AgentRole.CODER,
                summary=detail,
                payload={
                    "reason": self.failure_reason,
                    "content": detail,
                    "changed_files": sorted(self.changed_files),
                    "repair_cycles": self.repair_cycles,
                },
            ))
            return False

    def _has_structured_completion_evidence(self) -> bool:
        """新任务要求写入后验证；续跑任务允许对已保留文件直接验证后完成。"""
        successful_writes = [index for index, (name, ok) in enumerate(self.execution_evidence) if name == "write_file" and ok]
        if not successful_writes and not self.resume_existing:
            return False
        evidence_start = successful_writes[-1] if successful_writes else -1
        validations = [(index, ok) for index, (name, ok) in enumerate(self.execution_evidence) if name == "run_command" and index > evidence_start]
        return bool(validations) and validations[-1][1]

    def _evidence_digest(self) -> str:
        """生成不可依赖模型记忆的精简事实账本，始终随审查上下文保留。"""
        lines = ["Runtime evidence (authoritative; never claim an action was absent when it is listed here):"]
        if self.changed_files:
            lines.append("Changed files: " + ", ".join(sorted(self.changed_files)))
        else:
            lines.append("Changed files: none")
        lines.append(f"Post-write successful verification: {self._has_structured_completion_evidence()}")
        relevant = self.evidence_details[-8:]
        if not relevant:
            lines.append("Write/command evidence: none")
        for index, item in enumerate(relevant, 1):
            if item["tool"] == "write_file":
                lines.append(f"{index}. write_file path={item.get('path', '?')} ok={item['ok']} code={item['code']}")
                continue
            output = " ".join(str(item.get("output", "")).split())
            if len(output) > 900:
                output = output[-900:]
            command = str(item.get("command", ""))
            if len(command) > 500:
                command = command[:499] + "…"
            lines.append(
                f"{index}. run_command role={item['role']} ok={item['ok']} code={item['code']} "
                f"exit_code={item.get('exit_code', 'unknown')} command={command!r} output_tail={output!r}"
            )
        return "\n".join(lines)[:6000]

    async def _run_role(self, role: AgentRole, phase_instruction: str = "", phase_payload: dict[str, Any] | None = None) -> bool:
        policy = ROLE_POLICY[role]
        if self.conversation_only:
            policy = {"tools": set(), "goal": "作为统一的 MossCode 助手直接回应用户，并遵循用户授权的称呼和偏好记忆。"}
        start_summary = "核对文件状态并遵循只读要求。" if role in {AgentRole.CODER, AgentRole.SINGLE} and not self.requires_change else policy["goal"]
        await self.publish(AgentEvent(type=EventType.AGENT_STARTED, session_id=self.session_id, role=role, summary=start_summary, payload={"execution_mode": self.execution_mode, **self.memory_metadata, **(phase_payload or {})}))
        language = "English" if self.locale == "en-US" else "Chinese"
        implementation_contract = ""
        if role in {AgentRole.CODER, AgentRole.SINGLE} and not self.conversation_only:
            if self.resume_existing:
                implementation_contract = (
                    " This is a continuation or retry and earlier files were intentionally retained. Inspect and verify the existing implementation first. "
                    "Write only when a concrete acceptance gap remains; do not rewrite correct files merely to manufacture a write event. "
                    "Run an appropriate verification before concluding."
                )
            else:
                implementation_contract = (
                    " If the user requests a code or file change, you MUST inspect the relevant files, "
                    "call write_file for the implementation, and call run_command for an appropriate verification. "
                    "Never claim an implementation is complete without a successful write_file result. "
                    "You may use at most 5 list/read/search calls before your first write_file; use the explorer handoff and then implement instead of repeatedly inspecting."
                )
        runtime_contract = (
            f" The backend is running on Windows. When verifying Python code, use the exact interpreter "
            f"{json.dumps(sys.executable)} instead of guessing python, python3, or py. "
            "The command tool already runs from the authorized workspace root: use cwd `.` unless a listed workspace subdirectory is required, and never invent an absolute cwd. "
            "Do not use Unix-only tail/head pipelines on Windows; command output is already truncated safely. "
            "Validate JavaScript with Node.js or a browser; never pass JavaScript source to Python compile()."
            if "run_command" in policy["tools"] else ""
        )
        review_contract = (
            " You are advisory: the root orchestrator, not you, owns the task outcome. The acceptance scope is frozen verbatim in the user message. "
            "You may reject only for (1) an explicit item in that frozen scope, (2) a regression caused by this turn, or (3) an actually failing verification. "
            "Never introduce optional conventions, style preferences, extra entry points, broader tests, or new requirements. On repair cycles, re-check only the previously open issue and regressions shown by the same verification; do not restart a full audit. "
            "Return exactly `VERDICT: PASS` plus one concise sentence when satisfied. Otherwise return exactly four fields: `VERDICT: NEEDS_WORK`, `BASIS: USER_REQUIREMENT|FAILED_VERIFICATION|REGRESSION`, `EVIDENCE: <file/line or command failure>`, and `ACTION: <one minimal fix>`. "
            "Runtime evidence in the user message is authoritative. Never claim a listed command was not run or a listed file was not changed. A search with zero matches is not proof when a direct file read or runtime evidence contradicts it."
            if role is AgentRole.REVIEWER else ""
        )
        custom_instruction = self.agent_config[role.value]["instruction"]
        custom_contract = f" User-configured role instruction: {custom_instruction}" if custom_instruction else ""
        identity = "You are the unified MossCode assistant." if self.conversation_only else f"You are MossCode's internal {role.value} agent."
        identity_contract = " Never expose or introduce yourself as planner, explorer, coder, reviewer, single agent, or internal agent. Speak to the user as one unified assistant. Treat explicit user naming and preference memory as binding unless it conflicts with the current request or safety rules. When preference memory conflicts with older conversation turns, its newest/current value wins."
        system = {"role": "system", "content": f"{identity} {policy['goal']} Only call permitted tools.{implementation_contract}{runtime_contract}{review_contract}{custom_contract}{identity_contract} Your final answer must be natural, direct, under 180 Chinese characters or 120 English words, and must not repeat earlier agents' plans. Reply in {language}."}
        handoff_text = "\n".join(self.handoffs[-3:]) or "No prior agent handoff."
        rules_text = self.project_rules or "No project-specific rules were found."
        repairing = bool((phase_payload or {}).get("repairing"))
        evidence_text = f"\n\n{self._evidence_digest()}" if role is AgentRole.REVIEWER or (role is AgentRole.CODER and repairing) else ""
        acceptance_text = ""
        if role is AgentRole.REVIEWER:
            acceptance_text = f"\n\nFrozen acceptance scope (verbatim; do not expand):\n{self.acceptance_contract}"
            if self.repair_cycles and self.open_review_issue:
                acceptance_text += f"\n\nOnly open issue from the previous review:\n{self.open_review_issue}"
        phase_text = f"\n\nCurrent phase instruction:\n{phase_instruction}" if phase_instruction else ""
        reference_text = f"\n\nReferenced earlier task that the current short instruction continues:\n{self.reference_task}" if self.is_continuation else ""
        messages: list[dict[str, Any]] = [system, {"role": "user", "content": f"Authorized conversation and preference memory (honor explicit user preferences unless superseded by the current request):\n{self.conversation_context}\n\nProject rules (read-only, highest local priority):\n{rules_text}\n\nCurrent user task: {self.task}{reference_text}\nPrior agent handoffs in this turn (reference only; do your own role):\n{handoff_text}{phase_text}{acceptance_text}{evidence_text}"}]
        permitted = [TOOL_SCHEMAS[name] for name in policy["tools"]]
        # Coder 已产生权威命令证据时，Reviewer 只读复核，不重复运行同一测试和触发审批。
        if role is AgentRole.REVIEWER and any(item["tool"] == "run_command" for item in self.evidence_details):
            permitted = [schema for schema in permitted if schema.get("function", {}).get("name") != "run_command"]
        if hasattr(self.tools, "mcp_schemas"):
            permitted.extend(await asyncio.to_thread(self.tools.mcp_schemas, role))
        repeated_calls: dict[str, int] = {}
        implementation_reminded = False
        successful_tools: list[str] = []
        coder_inspection_calls = 0
        role_writes_at_start = sum(1 for name, ok in self.execution_evidence if name == "write_file" and ok)
        inspection_limit = 3 if repairing else 5
        for _ in range(min(self.settings.max_turns, self.agent_config[role.value]["max_turns"])):
            if self.cancelled():
                raise asyncio.CancelledError
            role_has_written = sum(1 for name, ok in self.execution_evidence if name == "write_file" and ok) > role_writes_at_start
            active_tools = permitted
            if role is AgentRole.CODER and not role_has_written and coder_inspection_calls >= inspection_limit:
                allowed_after_inspection = {"write_file"} if repairing else {"write_file", "run_command"}
                active_tools = [schema for schema in permitted if schema.get("function", {}).get("name") in allowed_after_inspection]
            try:
                assistant = await self.client.complete(self.context_manager.trim_role_messages(messages), active_tools)
            except LLMError as error:
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, role=role, summary=str(error)))
                raise
            tool_calls = assistant.get("tool_calls") or []
            messages.append(assistant)
            if not tool_calls:
                summary = (assistant.get("content") or "角色未给出文字总结").strip()[:800]
                if role in {AgentRole.CODER, AgentRole.SINGLE} and self.requires_change and not self.changed_files and not self.resume_existing and not implementation_reminded:
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
                    inspection_call = name in INSPECTION_TOOLS or (name == "run_command" and is_inspection_command(str(arguments.get("command", ""))))
                    inspection_budget_exhausted = role is AgentRole.CODER and not role_has_written and inspection_call and coder_inspection_calls >= inspection_limit
                    if role is AgentRole.CODER and inspection_call:
                        # 重复调用同样消耗探查预算，否则 repeated_tool_call 会让角色无限保留读取工具。
                        coder_inspection_calls += 1
                    if repeated_calls[signature] > 2:
                        result = ToolResult(False, "repeated_tool_call", "", {"tool": name})
                    elif inspection_budget_exhausted:
                        result = ToolResult(False, "inspection_budget_exhausted", "Use the collected context now: call write_file for a concrete fix or run_command to verify retained files.", {"limit": inspection_limit})
                    else:
                        await self.publish(AgentEvent(type=EventType.TOOL_REQUESTED, session_id=self.session_id, role=role, summary=f"请求工具：{name}", payload={"tool": name, "arguments": arguments}))
                        result = await self._execute(role, name, arguments)
                payload = {"ok": result.ok, "code": result.code, "content": result.content, "meta": result.meta}
                if result.ok:
                    successful_tools.append(name)
                await self.publish(AgentEvent(type=EventType.TOOL_FINISHED, session_id=self.session_id, role=role, summary=f"工具完成：{name}（{result.code}）", payload={"tool": name, "arguments": arguments, "result": payload}))
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(payload, ensure_ascii=False)})
        if role is AgentRole.REVIEWER:
            try:
                forced = await self.client.complete(
                    self.context_manager.trim_role_messages(messages + [{"role": "user", "content": f"{self._evidence_digest()}\n\nStop using tools. Reconcile your conclusion with the frozen acceptance scope and authoritative runtime evidence. Return PASS, or the required four-field NEEDS_WORK report. Do not add a new requirement."}]),
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
            command = str(arguments.get("command", ""))
            if is_file_mutation_command(command):
                return ToolResult(False, "command_file_mutation_not_allowed", "Use write_file for every source/configuration change so Diff, checkpoints, and changed-file evidence remain complete.", {"command": command})
            if os.name == "nt" and re.search(r"(?:^|[|&])\s*(?:tail|head)\b", command, flags=re.IGNORECASE):
                return ToolResult(False, "unsupported_windows_shell_syntax", "Remove the Unix-only tail/head pipeline and run the command directly; output is truncated automatically.", {"command": command})
            cwd = str(arguments.get("cwd", "."))
            try:
                command_directory = self.tools.workspace.resolve(cwd)
            except WorkspaceError as error:
                return ToolResult(False, str(error), "Use cwd `.` or a listed directory inside the authorized workspace.", {"cwd": cwd})
            if not command_directory.is_dir():
                return ToolResult(False, "not_a_directory", "Use cwd `.` or a listed workspace directory.", {"cwd": cwd})
            if self.command_mode == "deny":
                return ToolResult(False, "command_permission_denied", "", {"command": arguments.get("command", "")})
            if self.command_mode == "ask":
                if self.request_command_approval is None:
                    return ToolResult(False, "command_approval_unavailable", "", {})
                allowed = await self.request_command_approval(role, command, cwd)
                if not allowed:
                    return ToolResult(False, "command_permission_denied", "", {"command": arguments.get("command", "")})
        try:
            result = await asyncio.to_thread(self.tools.call_mcp, role, name, arguments) if is_mcp else await asyncio.to_thread(getattr(self.tools, name), **arguments)
            if name in {"write_file", "run_command"}:
                self.execution_evidence.append((name, result.ok))
                self.evidence_details.append({
                    "tool": name,
                    "role": role.value,
                    "ok": result.ok,
                    "code": result.code,
                    "path": result.meta.get("path", arguments.get("path", "")),
                    "command": arguments.get("command", ""),
                    "exit_code": result.meta.get("exit_code"),
                    "output": result.content,
                })
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
