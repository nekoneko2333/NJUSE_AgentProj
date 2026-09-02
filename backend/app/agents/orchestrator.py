from __future__ import annotations

import json
import sys
import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.agents.roles import ROLE_POLICY
from app.agents.multi_workflow import DEFAULT_MULTI_LIMITS, MultiStage, MultiWorkflowState
from app.core.context import ContextManager
from app.core.models import AgentEvent, AgentRole, EventType
from app.core.settings import Settings
from app.llm.client import AcceptanceCriterion, LLMError, OpenAICompatibleClient, TaskAnalysis, TaskReview
from app.runtime.workspace import WorkspaceError
from app.tools.registry import ToolRegistry, ToolResult

Publish = Callable[[AgentEvent], Awaitable[None]]
CommandApproval = Callable[[AgentRole, str, str], Awaitable[bool]]

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {"type": "function", "function": {"name": "list_files", "description": "列出工作区某目录的直接子项。用于直接证明只读验收项时传 criterion_ids。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "criterion_ids": {"type": "array", "items": {"type": "string"}}}}}},
    "read_file": {"type": "function", "function": {"name": "read_file", "description": "读取 UTF-8 文本文件的指定行。用于直接证明只读验收项时传 criterion_ids。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}, "criterion_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["path"]}}},
    "search_text": {"type": "function", "function": {"name": "search_text", "description": "在工作区中检索文本。用于直接证明只读验收项时传 criterion_ids。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "criterion_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["query"]}}},
    "write_file": {"type": "function", "function": {"name": "write_file", "description": "创建文件或用完整内容替换整个文本文件。修改已有文件时 content 必须是完整文件，并传入最近一次 read_file 返回的 expected_sha256；criterion_ids 必须列出本次写入直接实现的验收项。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "expected_sha256": {"type": "string", "description": "已有文件最近一次读取得到的 sha256；新文件留空。"}, "criterion_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["path", "content"]}}},
    "replace_text": {"type": "function", "function": {"name": "replace_text", "description": "对已有文本文件做一次精确局部替换。old_text 必须只出现一次，并传入最近一次 read_file 返回的 expected_sha256；criterion_ids 必须列出本次修改直接实现的验收项。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_sha256": {"type": "string"}, "criterion_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["path", "old_text", "new_text", "expected_sha256"]}}},
    "run_command": {"type": "function", "function": {"name": "run_command", "description": "在工作区内运行测试或开发命令。criterion_ids 绑定该命令证明的验收项；同一条命令可以同时绑定文件验证项和命令执行项，系统会按验收项类型分别记账。evidence_kind 可标注 verification、action 或 inspection。禁止危险或破坏性命令。", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}, "criterion_ids": {"type": "array", "items": {"type": "string"}}, "evidence_kind": {"type": "string", "enum": ["verification", "action", "inspection"]}}, "required": ["command"]}}},
}
ROLE_TURN_LIMITS = {
    AgentRole.SINGLE: 12,
    AgentRole.PLANNER: DEFAULT_MULTI_LIMITS.planner_turns,
    AgentRole.EXPLORER: DEFAULT_MULTI_LIMITS.explorer_turns,
    AgentRole.CODER: DEFAULT_MULTI_LIMITS.coder_turns,
    AgentRole.REVIEWER: DEFAULT_MULTI_LIMITS.reviewer_calls,
}
MAX_REPAIR_CYCLES = DEFAULT_MULTI_LIMITS.repair_cycles
INSPECTION_TOOLS = {"list_files", "read_file", "search_text"}
WRITE_TOOLS = {"write_file", "replace_text"}
REVIEW_BASES = {"USER_REQUIREMENT", "FAILED_VERIFICATION", "REGRESSION"}
ORCHESTRATOR_PROTOCOL = "manager-checkpoint-v8-bounded-graph"


@dataclass(frozen=True)
class EvidenceRecord:
    sequence: int
    kind: str
    criterion_ids: tuple[str, ...]
    ok: bool
    tool: str
    write_version: int
    detail: dict[str, Any]

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
        fixed_multi_role = role != AgentRole.SINGLE.value
        normalized[role] = {
            "enabled": True if fixed_multi_role else bool(value.get("enabled", defaults["enabled"])),
            "max_turns": int(defaults["max_turns"]) if fixed_multi_role else max(1, min(30, max_turns)),
            "instruction": instruction,
        }
    normalized[AgentRole.SINGLE.value]["enabled"] = True
    return normalized


def resolve_agent_mode(requested_mode: str, analysis: TaskAnalysis) -> str:
    if requested_mode != "adaptive":
        return "single" if requested_mode == "single" else "multi"
    return analysis.adaptive_mode


def turn_has_successful_write(events: list[AgentEvent], turn_id: str | None) -> bool:
    """Use persisted tool evidence to decide whether a retry/continuation has retained edits."""
    if not turn_id:
        return False
    return any(
        event.turn_id == turn_id
        and event.type == EventType.TOOL_FINISHED
        and event.payload.get("tool") in WRITE_TOOLS
        and bool(event.payload.get("result", {}).get("ok"))
        for event in events
    )


def continuation_lineage_has_successful_write(events: list[AgentEvent], prior_turn_ids: list[str]) -> bool:
    """Follow consecutive continuation turns back to the last concrete task checkpoint."""
    for turn_id in prior_turn_ids:
        if turn_has_successful_write(events, turn_id):
            return True
        started = next(
            (
                event for event in reversed(events)
                if event.turn_id == turn_id and event.type == EventType.AGENT_STARTED
            ),
            None,
        )
        if not bool((started.payload.get("task_analysis", {}) if started else {}).get("continuation")):
            break
    return False


def contains_serialized_tool_call(content: str) -> bool:
    """识别模型写进正文的伪工具协议，避免把未执行的调用当成最终答复。"""
    normalized = content.lower()
    has_tool_marker = "tool_calls" in normalized or "tool_call" in normalized
    has_protocol_markup = "dsml" in normalized or "<tool" in normalized or "<｜" in normalized
    return has_tool_marker and has_protocol_markup


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
        r"\bif\s+(?:not\s+)?exist\b[^\r\n&|]*\b(?:copy|move|ren|del)\b",
        r"\bsed\s+-i\b",
        r"\.write_text\s*\(",
        r"\.write_bytes\s*\(",
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
    def __init__(self, session_id: str, task: str, tools: ToolRegistry, client: OpenAICompatibleClient, settings: Settings, publish: Publish, locale: str = "zh-CN", conversation_context: str = "", context_manager: ContextManager | None = None, command_mode: str = "auto", request_command_approval: CommandApproval | None = None, cancelled: Callable[[], bool] | None = None, project_rules: str = "", execution_mode: str = "multi", agent_config: dict[str, Any] | None = None, memory_metadata: dict[str, Any] | None = None, reference_task: str = "", resume_existing: bool = False, task_analysis: TaskAnalysis | None = None) -> None:
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
        self.evidence_ledger: list[EvidenceRecord] = []
        self.write_version = 0
        self.repair_cycles = 0
        self.command_mode = command_mode
        self.request_command_approval = request_command_approval
        self.cancelled = cancelled or (lambda: False)
        self.project_rules = project_rules
        self.task_analysis = task_analysis or TaskAnalysis()
        self.reference_task = reference_task.strip()
        self.is_continuation = self.task_analysis.continuation and bool(self.reference_task)
        self.effective_task = f"{self.reference_task}\n后续指令：{task}" if self.is_continuation else task
        # 验收范围由一次语义 API 固化为带 ID 的任务契约；后续角色只能引用，不能扩张。
        self.criteria: dict[str, AcceptanceCriterion] = {item.id: item for item in self.task_analysis.criteria}
        self.acceptance_contract = json.dumps([
            {"id": item.id, "description": item.description, "kind": item.kind, "verification_hint": item.verification_hint}
            for item in self.criteria.values()
        ], ensure_ascii=False)
        self.resume_existing = bool(resume_existing or self.is_continuation)
        # 不预先把自然语言裁成“无工具闲聊”。所有输入都遵循用户选择的执行模式，
        # 由模型根据任务本身决定是否需要工具；角色权限仍由后端白名单约束。
        self.execution_mode = resolve_agent_mode(execution_mode, self.task_analysis)
        self.agent_config = normalize_agent_config(agent_config)
        self.multi_workflow = MultiWorkflowState() if self.execution_mode == "multi" else None
        self.memory_metadata = memory_metadata or {}
        self.requires_change = self.task_analysis.requires_file_change
        self.requires_command = self.task_analysis.requires_command

    async def run(self) -> bool:
        if self.execution_mode == "single":
            roles = [AgentRole.SINGLE]
        else:
            # The multi-agent graph is structural, not user-configurable. Old persisted
            # role switches cannot silently remove planning, exploration, or review.
            roles = [AgentRole.PLANNER, AgentRole.EXPLORER, AgentRole.CODER, AgentRole.REVIEWER]
        has_reviewer = AgentRole.REVIEWER in roles
        implementers = {AgentRole.CODER, AgentRole.SINGLE}
        role_stages = {
            AgentRole.PLANNER: MultiStage.PLAN,
            AgentRole.EXPLORER: MultiStage.EXPLORE,
            AgentRole.CODER: MultiStage.IMPLEMENT,
        }
        for role in (item for item in roles if item is not AgentRole.REVIEWER):
            if self.cancelled():
                raise asyncio.CancelledError
            if self.multi_workflow is not None:
                self.multi_workflow.transition(role_stages[role])
            role_finished = await self._run_role(role)
            if not role_finished and role in {AgentRole.PLANNER, AgentRole.EXPLORER}:
                # 规划和探索只提供上下文，不应因总结超时阻断真正的实现阶段。
                continue
            if not role_finished and role in implementers and not has_reviewer and not self.criteria:
                self.failure_reason = f"{role.value}_incomplete"
                self._fail_multi_workflow()
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, role=role, summary="执行达到该角色轮次上限，尚未形成可验收结论。", payload={"reason": self.failure_reason, "changed_files": sorted(self.changed_files)}))
                return False
        if has_reviewer:
            if self.multi_workflow is not None:
                self.multi_workflow.transition(MultiStage.REVIEW)
            if not await self._review_with_repairs():
                self._fail_multi_workflow()
                return False
        if not has_reviewer:
            # 单 Agent 走直接完成门槛：真实写入/命令成功即可，不再把模型生成的
            # 验收项 ID 当成第二套编排协议。细粒度证据账本只服务多 Agent 复核。
            unmet = [] if self.execution_mode == "single" and self._single_hard_requirements_met() else self._unmet_criteria()
            if unmet:
                self.failure_reason = "verification_incomplete"
                self._fail_multi_workflow()
                await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, summary=self._unmet_summary(unmet), payload={"reason": self.failure_reason, "changed_files": sorted(self.changed_files), "execution_mode": self.execution_mode, "unmet_criteria": unmet, "criteria_status": self._criteria_status()}))
                return False
            final_role = roles[-1]
            self.review_detail = self.role_summaries.get(final_role, "任务已完成。")
            self.completion_source = "structured_evidence" if self.requires_change or self.requires_command else "agent_summary"
        finishing_role = AgentRole.REVIEWER if has_reviewer else roles[-1]
        if self.multi_workflow is not None:
            self.multi_workflow.transition(MultiStage.FINALIZE)
        hook_result = await self._run_hooks("before_finish", finishing_role)
        if hook_result is not None and not hook_result.ok:
            self.failure_reason = "before_finish_hook_failed"
            self._fail_multi_workflow()
            await self.publish(AgentEvent(type=EventType.TASK_FAILED, session_id=self.session_id, summary="结束前 Hook 未通过，改动已保留。", payload={"reason": self.failure_reason, "content": hook_result.content}))
            return False
        user_result = self._user_facing_summary()
        if self.changed_files:
            file_list = "\n".join(f"- `{path}`" for path in sorted(self.changed_files))
            summary = f"{user_result}\n\n变更文件：\n{file_list}"
        elif self.requires_command:
            summary = user_result
        elif not self.requires_change:
            summary = user_result
        else:
            summary = "执行流程已经结束，但本次没有写入文件；请检查执行说明和工具记录。"
        if self.multi_workflow is not None:
            self.multi_workflow.transition(MultiStage.DONE)
        await self.publish(AgentEvent(
            type=EventType.TASK_FINISHED,
            session_id=self.session_id,
            summary=summary,
            payload={"changed_files": sorted(self.changed_files), "completion_source": self.completion_source, "verification_status": "passed", "execution_mode": self.execution_mode, "intent": "command" if self.requires_command else "change" if self.requires_change else "general", "repair_cycles": self.repair_cycles, "criteria_status": self._criteria_status(), "multi_agent_limits": DEFAULT_MULTI_LIMITS.public_dict() if self.multi_workflow is not None else {}, **self._workflow_payload()},
        ))
        return True

    def _workflow_payload(self) -> dict[str, Any]:
        return self.multi_workflow.payload() if self.multi_workflow is not None else {}

    def _fail_multi_workflow(self) -> None:
        if self.multi_workflow is not None:
            self.multi_workflow.fail()

    def _user_facing_summary(self) -> str:
        """Reviewer output is control-plane data; only a working agent may address the user."""
        for role in (AgentRole.SINGLE, AgentRole.CODER, AgentRole.EXPLORER, AgentRole.PLANNER):
            summary = self.role_summaries.get(role, "").strip()
            if summary:
                return summary
        if self.changed_files:
            return f"已完成本轮任务，共修改 {len(self.changed_files)} 个文件。"
        if self.requires_command:
            return "命令已经执行并完成验证。"
        return "检查已经完成。"

    async def _review_with_repairs(self) -> bool:
        """Production path: one structured review, then at most one evidence-scoped repair."""
        if not hasattr(self.client, "review_task"):
            return await self._legacy_review_with_repairs()

        await self.publish(AgentEvent(
            type=EventType.AGENT_STARTED,
            session_id=self.session_id,
            role=AgentRole.REVIEWER,
            summary="正在按验收项核对证据账本。",
            payload={"criteria_status": self._criteria_status(), "structured_review": True, **self._workflow_payload()},
        ))
        try:
            review: TaskReview = await self.client.review_task(
                analysis=self.task_analysis,
                evidence=self._review_evidence_payload(),
            )
        except LLMError:
            unmet = self._unmet_criteria()
            if not unmet:
                self.completion_source = "criterion_evidence"
                self.review_detail = "结构化审查响应无效；全部验收项已有独立成功证据，已按证据账本完成。"
                return True
            return await self._fail_unmet_criteria(unmet, "结构化审查响应无效，且仍有验收项缺少证据。")

        review_payload = {
            "assessments": [
                {"criterion_id": item.criterion_id, "status": item.status, "evidence": item.evidence, "action": item.action}
                for item in review.assessments
            ],
            "summary": review.summary,
        }
        await self.publish(AgentEvent(
            type=EventType.AGENT_FINISHED,
            session_id=self.session_id,
            role=AgentRole.REVIEWER,
            summary=review.summary or "结构化验收已完成。",
            payload={"structured_review": True, **review_payload, **self._workflow_payload()},
        ))

        ledger_unmet = set(self._unmet_criteria())
        reviewer_unmet = {item.criterion_id for item in review.assessments if item.status != "satisfied"}
        open_ids = ledger_unmet | reviewer_unmet
        if not open_ids:
            self.completion_source = "criterion_review"
            self.review_detail = review.summary or "所有验收项及其证据均已通过。"
            return True

        if self.repair_cycles < MAX_REPAIR_CYCLES:
            self.repair_cycles += 1
            if self.multi_workflow is not None:
                self.multi_workflow.transition(MultiStage.REPAIR)
            repair_start = len(self.evidence_ledger)
            actions = [
                f"{item.criterion_id}: {item.action or item.evidence or '补齐该验收项的实现和验证证据'}"
                for item in review.assessments if item.criterion_id in open_ids
            ]
            known = {item.criterion_id for item in review.assessments}
            actions.extend(f"{criterion_id}: 补齐该验收项的工具证据" for criterion_id in sorted(open_ids - known))
            self.open_review_issue = "\n".join(actions)
            await self._run_role(
                AgentRole.CODER,
                phase_instruction=(
                    "这是唯一一次内部返工。只处理下列仍未闭环的验收项，不重新审计其他范围：\n"
                    f"{self.open_review_issue}\n"
                    "每个写入和验证调用都必须携带对应 criterion_ids。"
                ),
                phase_payload={"repair_cycle": self.repair_cycles, "repairing": True, "criterion_ids": sorted(open_ids)},
            )
            still_unmet = set(self._unmet_criteria())
            # Reviewer 指出的语义缺口必须在返工开始后产生一组新的闭环证据，旧证据不能原样复用。
            still_unmet.update(
                criterion_id for criterion_id in reviewer_unmet
                if not self._criterion_satisfied(criterion_id, after_sequence=repair_start, allow_resume=False)
            )
            if not still_unmet:
                self.completion_source = "criterion_evidence_after_repair"
                self.review_detail = "一次定向返工后，所有验收项均已形成新的闭环证据。"
                return True
            return await self._fail_unmet_criteria(sorted(still_unmet), "定向返工后仍有验收项没有形成闭环证据。")

        return await self._fail_unmet_criteria(sorted(open_ids), "仍有验收项没有形成闭环证据。")

    async def _legacy_review_with_repairs(self) -> bool:
        """Compatibility path for injected clients that predate the structured review API."""
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
                if self._has_post_write_successful_validation():
                    # Reviewer 已结合失败输出判断其与冻结验收范围无关；保留失败证据，
                    # 但不因“最后一个命令非零”机械重跑整套 Coder/Reviewer 循环。
                    self.completion_source = "reviewer_mixed_evidence"
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
                if self.multi_workflow is not None:
                    self.multi_workflow.transition(MultiStage.REPAIR)
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
                if self.multi_workflow is not None:
                    self.multi_workflow.transition(MultiStage.REVIEW)
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
            self._fail_multi_workflow()
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
                    **self._workflow_payload(),
                },
            ))
            return False

    def _criterion_satisfied(self, criterion_id: str, *, after_sequence: int = 0, allow_resume: bool = True) -> bool:
        criterion = self.criteria.get(criterion_id)
        if criterion is None:
            return False
        if (
            self.execution_mode == "single"
            and criterion.kind == "file_change"
            and not self.changed_files
            and self._single_runtime_resolution_completed()
        ):
            return True
        records = [
            item for item in self.evidence_ledger
            if criterion_id in item.criterion_ids and item.sequence > after_sequence
        ]
        if criterion.kind == "file_change":
            writes = [item for item in records if item.kind == "write" and item.ok]
            if not writes:
                if not (allow_resume and self.resume_existing):
                    return False
                write_version = 0
            else:
                write_version = writes[-1].write_version
            validations = [
                item for item in records
                if item.kind == "verification" and item.write_version >= write_version
            ]
            return bool(validations) and validations[-1].ok
        expected_kind = {"command": "action", "inspection": "inspection", "response": "response"}[criterion.kind]
        matching = [item for item in records if item.kind == expected_kind]
        return bool(matching) and matching[-1].ok

    def _criteria_status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "description": item.description,
                "kind": item.kind,
                "satisfied": self._criterion_satisfied(item.id),
            }
            for item in self.criteria.values()
        ]

    def _unmet_criteria(self) -> list[str]:
        return [item.id for item in self.criteria.values() if not self._criterion_satisfied(item.id)]

    def _unmet_summary(self, criterion_ids: list[str] | set[str]) -> str:
        descriptions = [self.criteria[item].description for item in criterion_ids if item in self.criteria]
        detail = "；".join(descriptions) or "未知验收项"
        return f"任务尚未完成：以下验收项缺少闭环证据——{detail}。现有改动已保留。"

    async def _fail_unmet_criteria(self, criterion_ids: list[str] | set[str], detail: str) -> bool:
        ids = sorted(criterion_ids)
        self.failure_reason = "acceptance_evidence_incomplete"
        self._fail_multi_workflow()
        await self.publish(AgentEvent(
            type=EventType.TASK_FAILED,
            session_id=self.session_id,
            role=AgentRole.CODER,
            summary=f"{detail}\n{self._unmet_summary(ids)}",
            payload={
                "reason": self.failure_reason,
                "changed_files": sorted(self.changed_files),
                "repair_cycles": self.repair_cycles,
                "unmet_criteria": ids,
                "criteria_status": self._criteria_status(),
                **self._workflow_payload(),
            },
        ))
        return False

    def _review_evidence_payload(self) -> dict[str, Any]:
        criteria_status = self._criteria_status()
        records: list[dict[str, Any]] = []
        used = len(json.dumps(criteria_status, ensure_ascii=False))
        for item in reversed(self.evidence_ledger):
            detail = {
                key: value[:500] if isinstance(value, str) else value
                for key, value in item.detail.items()
            }
            record = {
                "sequence": item.sequence,
                "kind": item.kind,
                "criterion_ids": list(item.criterion_ids),
                "ok": item.ok,
                "tool": item.tool,
                "write_version": item.write_version,
                **detail,
            }
            size = len(json.dumps(record, ensure_ascii=False))
            if records and used + size > DEFAULT_MULTI_LIMITS.evidence_chars:
                break
            records.insert(0, record)
            used += size
        return {"criteria_status": criteria_status, "records": records}

    def _has_structured_completion_evidence(self) -> bool:
        """新任务要求写入后验证；续跑任务允许对已保留文件直接验证后完成。"""
        successful_writes = [index for index, (name, ok) in enumerate(self.execution_evidence) if name in WRITE_TOOLS and ok]
        if not successful_writes and not self.resume_existing:
            return False
        evidence_start = successful_writes[-1] if successful_writes else -1
        validations = [(index, ok) for index, (name, ok) in enumerate(self.execution_evidence) if name == "run_command" and index > evidence_start]
        return bool(validations) and validations[-1][1]

    def _has_post_write_successful_validation(self) -> bool:
        successful_writes = [index for index, (name, ok) in enumerate(self.execution_evidence) if name in WRITE_TOOLS and ok]
        if not successful_writes and not self.resume_existing:
            return False
        evidence_start = successful_writes[-1] if successful_writes else -1
        return any(name == "run_command" and ok and index > evidence_start for index, (name, ok) in enumerate(self.execution_evidence))

    def _evidence_digest(self) -> str:
        """生成不可依赖模型记忆的精简事实账本，始终随审查上下文保留。"""
        lines = ["Runtime evidence (authoritative; never claim an action was absent when it is listed here):"]
        if self.changed_files:
            lines.append("Changed files: " + ", ".join(sorted(self.changed_files)))
        else:
            lines.append("Changed files: none")
        lines.append(f"Post-write successful verification: {self._has_structured_completion_evidence()}")
        relevant = self.evidence_details[-8:]
        latest_write = next((item for item in reversed(self.evidence_details) if item["tool"] in WRITE_TOOLS and item["ok"]), None)
        if latest_write is not None and latest_write not in relevant:
            relevant = [latest_write, *relevant[-7:]]
        if not relevant:
            lines.append("Write/command evidence: none")
        for index, item in enumerate(relevant, 1):
            if item["tool"] in WRITE_TOOLS:
                lines.append(f"{index}. {item['tool']} path={item.get('path', '?')} ok={item['ok']} code={item['code']}")
                diff = str(item.get("diff", ""))
                if diff:
                    lines.append("   diff=" + " ".join(diff.split())[:1200])
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
        limit = DEFAULT_MULTI_LIMITS.evidence_chars if self.multi_workflow is not None else 6000
        return "\n".join(lines)[:limit]

    def _has_successful_command(self) -> bool:
        commands = [item for item in self.evidence_details if item["tool"] == "run_command"]
        return bool(commands) and bool(commands[-1]["ok"])

    def _single_hard_requirements_met(self) -> bool:
        """单 Agent 只保留用户能理解的硬门槛，不要求模型维护验收证据图。"""
        if self.requires_change and not (
            self._has_structured_completion_evidence() or self._single_runtime_resolution_completed()
        ):
            return False
        if self.requires_command and not self._has_successful_command():
            return False
        return True

    def _single_runtime_resolution_completed(self) -> bool:
        """磁盘代码正确但旧进程未重载时，允许“运行时动作→现场验证”闭环。"""
        commands = [item for item in self.evidence_details if item["tool"] == "run_command" and item["ok"]]
        action_indexes = [
            index for index, item in enumerate(commands)
            if item.get("evidence_kind") == "action"
        ]
        if not action_indexes:
            return False
        return any(
            index > action_indexes[-1] and item.get("evidence_kind") in {"verification", "inspection"}
            for index, item in enumerate(commands)
        )

    def _single_fallback_summary(self) -> str:
        """工具已经完成时，即使模型耗尽轮次也返回事实摘要，而不是制造假失败。"""
        successful_commands = [
            item for item in self.evidence_details
            if item["tool"] == "run_command" and item["ok"]
        ]
        if successful_commands:
            latest = successful_commands[-1]
            material = f"{latest.get('command', '')}\n{latest.get('output', '')}"
            urls = re.findall(r"https?://[^\s'\"`)]+", material)
            if urls:
                return f"命令已成功执行并完成验证。访问地址：{urls[-1]}"
            return "命令已成功执行并完成验证，真实结果已保留在终端记录中。"
        if self.changed_files:
            return f"修改和验证已经完成，共更新 {len(self.changed_files)} 个文件。"
        return "检查已经完成。"

    async def _run_role(self, role: AgentRole, phase_instruction: str = "", phase_payload: dict[str, Any] | None = None) -> bool:
        policy = ROLE_POLICY[role]
        if role in {AgentRole.CODER, AgentRole.SINGLE} and self.requires_command:
            start_summary = "正在执行用户要求的命令并核对真实结果。"
        elif role in {AgentRole.CODER, AgentRole.SINGLE} and not self.requires_change:
            start_summary = "核对文件状态并遵循只读要求。"
        else:
            start_summary = policy["goal"]
        analysis_payload = {
            "continuation": self.task_analysis.continuation,
            "requires_file_change": self.requires_change,
            "requires_command": self.requires_command,
            "adaptive_mode": self.task_analysis.adaptive_mode,
            "criteria": [
                {"id": item.id, "description": item.description, "kind": item.kind, "verification_hint": item.verification_hint}
                for item in self.criteria.values()
            ],
        }
        await self.publish(AgentEvent(type=EventType.AGENT_STARTED, session_id=self.session_id, role=role, summary=start_summary, payload={"execution_mode": self.execution_mode, "task_analysis": analysis_payload, **self.memory_metadata, **(phase_payload or {}), **self._workflow_payload()}))
        language = "English" if self.locale == "en-US" else "Chinese"
        implementation_contract = ""
        if role in {AgentRole.CODER, AgentRole.SINGLE}:
            if self.resume_existing:
                implementation_contract = (
                    " This is a continuation or retry and earlier files were intentionally retained. Inspect and verify the existing implementation first. "
                    "Write only when a concrete acceptance gap remains; do not rewrite correct files merely to manufacture a write event. "
                    "Run an appropriate verification before concluding."
                )
            else:
                implementation_contract = (
                    " If the user requests a code or file change, you MUST inspect the relevant files, "
                    "call replace_text for a focused edit or write_file for a new/full file, then call run_command for an appropriate verification. "
                    "Never claim an implementation is complete without a successful write tool result. "
                    "write_file replaces the entire target: read an existing file completely, reuse its returned sha256 as expected_sha256, and send its complete updated content; never overwrite a page or source file with a partial function or snippet. "
                    f"You may use at most {DEFAULT_MULTI_LIMITS.coder_inspection_calls if role is AgentRole.CODER else 5} list/read/search calls before your first write; use the explorer handoff and then implement instead of repeatedly inspecting."
                )
            if self.requires_command:
                implementation_contract += (
                    " The user requested an actual command action. You MUST call run_command and report its real result; "
                    "a plan, promise, or serialized tool markup is not completion."
                )
            implementation_contract += (
                " Validation must assert the user-visible behavior or returned content, not merely process existence or an HTTP success status. "
                "If the response reproduces the reported bad content, the defect remains."
            )
            if role is AgentRole.SINGLE:
                implementation_contract += (
                    " Keep the workflow direct: perform the necessary tools, then answer naturally. criterion_ids are optional bookkeeping; "
                    "never attach a response criterion to a tool call because response criteria are satisfied only by your final answer. "
                    "If source/tests are already correct but a live service is stale, do not edit a correct file just to satisfy the plan: "
                    "restart or reload it with evidence_kind=action, then check the live user-visible result with evidence_kind=verification."
                )
            else:
                implementation_contract += (
                    " Every write and verification must include the exact criterion_ids it proves; "
                    "do not attach an unrelated failure to a criterion that already has a targeted successful verification."
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
            "Runtime evidence in the user message is authoritative. Never claim a listed command was not run or a listed file was not changed. A search with zero matches is not proof when a direct file read or runtime evidence contradicts it. "
            "HTTP 200 proves reachability only, not correct page content. Reproduce the reported user-visible symptom and inspect the response structure/content. If the server response itself contains the bad content, never blame browser cache and never PASS."
            if role is AgentRole.REVIEWER else ""
        )
        custom_instruction = self.agent_config[role.value]["instruction"]
        custom_contract = f" User-configured role instruction: {custom_instruction}" if custom_instruction else ""
        identity = f"You are MossCode's internal {role.value} agent."
        identity_contract = " Never expose or introduce yourself as planner, explorer, coder, reviewer, single agent, or internal agent. Speak to the user as one unified assistant. Treat explicit user naming and preference memory as binding unless it conflicts with the current request or safety rules. When preference memory conflicts with older conversation turns, its newest/current value wins. Never invent a lack of permissions or tools: use the tools actually provided to this role, and report the exact tool result when an action is denied or fails."
        system = {"role": "system", "content": f"{identity} {policy['goal']} Only call permitted tools.{implementation_contract}{runtime_contract}{review_contract}{custom_contract}{identity_contract} Your final answer must be natural, direct, under 180 Chinese characters or 120 English words, and must not repeat earlier agents' plans. Reply in {language}."}
        handoff_text = "\n".join(self.handoffs[-3:]) or "No prior agent handoff."
        if self.multi_workflow is not None:
            handoff_text = handoff_text[-DEFAULT_MULTI_LIMITS.handoff_chars:]
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
        permitted = [] if role is AgentRole.REVIEWER else [TOOL_SCHEMAS[name] for name in policy["tools"]]
        if role is not AgentRole.REVIEWER and hasattr(self.tools, "mcp_schemas"):
            permitted.extend(await asyncio.to_thread(self.tools.mcp_schemas, role))
        repeated_calls: dict[str, int] = {}
        implementation_reminded = False
        execution_reminded = False
        criteria_reminded = False
        phase_transition_announced = False
        successful_tools: list[str] = []
        total_tool_calls = 0
        coder_inspection_calls = 0
        role_writes_at_start = sum(1 for name, ok in self.execution_evidence if name in WRITE_TOOLS and ok)
        if self.multi_workflow is not None:
            inspection_limit = DEFAULT_MULTI_LIMITS.repair_inspection_calls if repairing else DEFAULT_MULTI_LIMITS.coder_inspection_calls
            role_turn_limit = DEFAULT_MULTI_LIMITS.role_turns(role, repairing=repairing)
            role_tool_limit: int | None = DEFAULT_MULTI_LIMITS.role_tool_calls(role, repairing=repairing)
        else:
            inspection_limit = 3 if repairing else 5
            role_turn_limit = min(self.settings.max_turns, self.agent_config[role.value]["max_turns"])
            role_tool_limit = None
        for _ in range(role_turn_limit):
            if self.cancelled():
                raise asyncio.CancelledError
            role_has_written = sum(1 for name, ok in self.execution_evidence if name in WRITE_TOOLS and ok) > role_writes_at_start
            active_tools = permitted
            if role in {AgentRole.CODER, AgentRole.SINGLE} and coder_inspection_calls >= inspection_limit:
                allowed_after_inspection = {*WRITE_TOOLS, "run_command"} if not repairing or role_has_written else WRITE_TOOLS
                active_tools = []
                for schema in permitted:
                    tool_name = schema.get("function", {}).get("name")
                    if tool_name not in allowed_after_inspection:
                        continue
                    restricted = json.loads(json.dumps(schema))
                    if tool_name == "run_command":
                        function = restricted["function"]
                        function["description"] = "探查阶段已结束。只运行实现验证或用户要求的真实动作，禁止再读取/搜索文件。"
                        parameters = function["parameters"]
                        parameters["properties"]["evidence_kind"]["enum"] = ["verification", "action"]
                        if role is not AgentRole.SINGLE:
                            parameters["required"] = list(dict.fromkeys([*parameters.get("required", []), "criterion_ids", "evidence_kind"]))
                    active_tools.append(restricted)
            if role_tool_limit is not None and total_tool_calls >= role_tool_limit:
                active_tools = []
            try:
                assistant = await self.client.complete(self.context_manager.trim_role_messages(messages), active_tools)
            except LLMError as error:
                await self.publish(AgentEvent(
                    type=EventType.TASK_FAILED,
                    session_id=self.session_id,
                    role=role,
                    summary=error.user_message(self.locale),
                    payload={"reason": error.code, "status_code": error.status_code, "retryable": error.retryable, "stage": "agent_run"},
                ))
                raise
            raw_tool_calls = assistant.get("tool_calls") or []
            dropped_tool_calls = 0
            tool_calls = raw_tool_calls
            if role_tool_limit is not None:
                remaining_tool_calls = max(0, role_tool_limit - total_tool_calls)
                tool_calls = raw_tool_calls[:remaining_tool_calls]
                dropped_tool_calls = len(raw_tool_calls) - len(tool_calls)
            messages.append({**assistant, "tool_calls": tool_calls} if dropped_tool_calls else assistant)
            if dropped_tool_calls:
                budget_payload = {
                    "ok": False,
                    "code": "role_tool_budget_exhausted",
                    "content": "Extra tool calls were discarded before execution.",
                    "meta": {"limit": role_tool_limit, "dropped_calls": dropped_tool_calls},
                }
                await self.publish(AgentEvent(
                    type=EventType.TOOL_FINISHED,
                    session_id=self.session_id,
                    role=role,
                    summary=f"已截断 {dropped_tool_calls} 个超出固定预算的工具调用。",
                    payload={"tool": "workflow_budget", "arguments": {}, "result": budget_payload, "role_tool_calls": total_tool_calls, "role_tool_call_limit": role_tool_limit, **self._workflow_payload()},
                ))
            if not tool_calls:
                if dropped_tool_calls:
                    messages.append({"role": "user", "content": "The fixed tool-call budget is exhausted. Do not request more tools; return a concise handoff based on the retained results."})
                    continue
                summary = (assistant.get("content") or "角色未给出文字总结").strip()[:800]
                if contains_serialized_tool_call(summary):
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous response serialized a tool call as visible text, so nothing was executed. "
                            "Do not repeat DSML/XML/tool-call markup. Use the native tool-calling interface now; "
                            "if no tools are available, reply with a plain natural-language answer instead."
                        ),
                    })
                    continue
                if role in {AgentRole.CODER, AgentRole.SINGLE} and self.requires_change and not self.changed_files and not self.resume_existing and not implementation_reminded:
                    implementation_reminded = True
                    messages.append({"role": "user", "content": "The requested change is not implemented yet. Use replace_text for a focused edit or write_file for a full/new file, then verify the result. Do not repeat the plan."})
                    continue
                if role in {AgentRole.CODER, AgentRole.SINGLE} and self.requires_command and not self._has_successful_command() and not execution_reminded:
                    execution_reminded = True
                    messages.append({"role": "user", "content": "The user requested an actual command action, but no command has executed successfully. Call run_command now and base the final answer on its real result. Do not merely promise to run it."})
                    continue
                if role is AgentRole.SINGLE and self.requires_change and not (
                    self._has_structured_completion_evidence() or self._single_runtime_resolution_completed()
                ) and not criteria_reminded:
                    criteria_reminded = True
                    messages.append({
                        "role": "user",
                        "content": "The file change is present, but it still needs one successful final verification. Run the relevant test or check once, then answer the user directly.",
                    })
                    continue
                # 当前自然语言内容本身就是 response 验收项的证据。单 Agent 不应
                # 因为“回复还没有被记录”而被要求再调用一个毫不相关的工具。
                unmet = [] if role is AgentRole.SINGLE else self._unmet_criteria()
                if role in {AgentRole.CODER, AgentRole.SINGLE} and unmet and not criteria_reminded:
                    criteria_reminded = True
                    descriptions = "; ".join(f"{item}: {self.criteria[item].description}" for item in unmet)
                    messages.append({
                        "role": "user",
                        "content": (
                            f"These frozen criteria still lack typed evidence: {descriptions}. "
                            "Use the necessary tool now and include the exact criterion_ids. A single run_command may bind both a retained file-change criterion and a command criterion. "
                            "Do not repeat repository inspection or return a summary until these ids are closed."
                        ),
                    })
                    continue
                self.handoffs.append(f"{role.value}: {summary}")
                self.role_summaries[role] = summary
                if role in {AgentRole.CODER, AgentRole.SINGLE}:
                    self._record_response_evidence(role, summary)
                await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary, "role_tool_calls": total_tool_calls, "role_tool_call_limit": role_tool_limit, **self._workflow_payload()}))
                return True
            for call in tool_calls:
                if self.cancelled():
                    raise asyncio.CancelledError
                name = call.get("function", {}).get("name", "")
                arguments: dict[str, Any] = {}
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError:
                    result = ToolResult(False, "invalid_tool_arguments", "", {})
                else:
                    if name in {"exec_command", "execute_command"}:
                        name = "run_command"
                        arguments = {
                            "command": str(arguments.get("command", arguments.get("cmd", ""))),
                            "cwd": str(arguments.get("cwd", ".")),
                        }
                    signature = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
                    repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
                    declared_inspection = str(arguments.get("evidence_kind") or "").strip() == "inspection"
                    inspection_call = name in INSPECTION_TOOLS or (
                        name == "run_command"
                        and (declared_inspection or ("evidence_kind" not in arguments and is_inspection_command(str(arguments.get("command", "")))))
                    )
                    inspection_budget_exhausted = role in {AgentRole.CODER, AgentRole.SINGLE} and inspection_call and coder_inspection_calls >= inspection_limit
                    if role in {AgentRole.CODER, AgentRole.SINGLE} and inspection_call:
                        # 重复调用同样消耗探查预算，否则 repeated_tool_call 会让角色无限保留读取工具。
                        coder_inspection_calls += 1
                    tool_budget_exhausted = role_tool_limit is not None and total_tool_calls >= role_tool_limit
                    if not tool_budget_exhausted:
                        total_tool_calls += 1
                    if tool_budget_exhausted:
                        result = ToolResult(False, "role_tool_budget_exhausted", "The fixed tool-call budget for this role is exhausted. Return a concise handoff now.", {"limit": role_tool_limit})
                    elif repeated_calls[signature] > 2:
                        result = ToolResult(False, "repeated_tool_call", "", {"tool": name})
                    elif inspection_budget_exhausted:
                        result = ToolResult(False, "inspection_budget_exhausted", "Use the collected context now: call replace_text/write_file for a concrete fix or run_command to verify retained files.", {"limit": inspection_limit})
                    else:
                        await self.publish(AgentEvent(type=EventType.TOOL_REQUESTED, session_id=self.session_id, role=role, summary=f"请求工具：{name}", payload={"tool": name, "arguments": arguments}))
                        result = await self._execute(role, name, arguments)
                payload = {"ok": result.ok, "code": result.code, "content": result.content, "meta": result.meta}
                if result.ok:
                    successful_tools.append(name)
                await self.publish(AgentEvent(type=EventType.TOOL_FINISHED, session_id=self.session_id, role=role, summary=f"工具完成：{name}（{result.code}）", payload={"tool": name, "arguments": arguments, "result": payload, "role_tool_calls": total_tool_calls, "role_tool_call_limit": role_tool_limit, **self._workflow_payload()}))
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": json.dumps(payload, ensure_ascii=False)})
            if dropped_tool_calls:
                messages.append({"role": "user", "content": "Additional tool calls were discarded because this role reached its hard budget. Summarize the retained results now; do not request more tools."})
            if (
                role in {AgentRole.CODER, AgentRole.SINGLE}
                and coder_inspection_calls >= inspection_limit
                and not phase_transition_announced
            ):
                phase_transition_announced = True
                pending = self._unmet_criteria()
                messages.append({
                    "role": "user",
                    "content": (
                        "DISCOVERY PHASE CLOSED. Repository inspection is no longer available in this run. "
                        f"Unmet criterion ids: {', '.join(pending) or 'none'}. "
                        "Now either edit with replace_text/write_file, or verify retained work with one run_command bound to every criterion it proves. "
                        "Do not issue another inspection command."
                    ),
                })
        if role is AgentRole.SINGLE and self._single_hard_requirements_met():
            try:
                forced = await self.client.complete(
                    self.context_manager.trim_role_messages(messages + [{
                        "role": "user",
                        "content": "Stop using tools. The required action has succeeded. Give the user one direct natural-language result based only on the actual tool output, including any final URL or command result.",
                    }]),
                    [],
                )
            except LLMError:
                forced = {}
            summary = str(forced.get("content") or "").strip()[:800] or self._single_fallback_summary()
            self.handoffs.append(f"{role.value}: {summary}")
            self.role_summaries[role] = summary
            self._record_response_evidence(role, summary)
            await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary, "reason": "forced_summary", "role_tool_calls": total_tool_calls, "role_tool_call_limit": role_tool_limit, **self._workflow_payload()}))
            return True
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
                    await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary, "reason": "forced_summary", "role_tool_calls": total_tool_calls, "role_tool_call_limit": role_tool_limit, **self._workflow_payload()}))
                    return True
            except LLMError:
                pass
        if self.locale == "en-US":
            summary = "This role reached its turn limit after completing tool operations; review the tool records for the retained results." if successful_tools else "This role reached its turn limit before producing a final summary."
        else:
            summary = "本角色已完成工具操作，但在整理总结前达到轮次上限；结果已保留在工具记录中。" if successful_tools else "本角色在给出最终总结前达到轮次上限，请检查工具记录。"
        self.handoffs.append(f"{role.value}: {summary}")
        self.role_summaries[role] = summary
        await self.publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=self.session_id, role=role, summary=summary, payload={"content": summary, "reason": "turn_limit", "role_tool_calls": total_tool_calls, "role_tool_call_limit": role_tool_limit, **self._workflow_payload()}))
        return False

    async def _execute(self, role: AgentRole, name: str, arguments: dict[str, Any]) -> ToolResult:
        is_mcp = "/" in name
        if not is_mcp and (name not in ROLE_POLICY[role]["tools"] or not hasattr(self.tools, name)):
            return ToolResult(False, "tool_not_permitted", "", {})
        criterion_ids, evidence_kind, binding_error = self._resolve_evidence_binding(name, arguments)
        if binding_error:
            if role is AgentRole.SINGLE:
                # 单 Agent 的 criterion_ids 只是可选元数据。模型标错 ID 不应阻止
                # 一条本来合法、安全的文件或命令工具实际执行。
                requested_kind = str(arguments.get("evidence_kind") or "").strip()
                criterion_ids = ()
                evidence_kind = requested_kind if requested_kind in {"verification", "action", "inspection"} else ""
            else:
                return ToolResult(False, binding_error, "Bind this tool call to the relevant typed acceptance criterion_ids.", {"criterion_ids": list(criterion_ids)})
        runtime_arguments = dict(arguments)
        runtime_arguments.pop("criterion_ids", None)
        runtime_arguments.pop("evidence_kind", None)
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
                result = ToolResult(False, "command_file_mutation_not_allowed", "Use replace_text or write_file so Diff, checkpoints, and changed-file evidence remain complete.", {"command": command})
                self._record_execution_evidence(role, name, arguments, result, criterion_ids, evidence_kind)
                return result
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
            requires_approval = self.tools.command_requires_approval(command)
            if self.command_mode == "ask" or requires_approval:
                if self.request_command_approval is None:
                    return ToolResult(False, "command_approval_unavailable", "", {})
                allowed = await self.request_command_approval(role, command, cwd)
                if not allowed:
                    return ToolResult(False, "command_permission_denied", "", {"command": arguments.get("command", "")})
                if requires_approval:
                    runtime_arguments["approved"] = True
        try:
            result = await asyncio.to_thread(self.tools.call_mcp, role, name, runtime_arguments) if is_mcp else await asyncio.to_thread(getattr(self.tools, name), **runtime_arguments)
            if name in WRITE_TOOLS or name == "run_command" or (name in INSPECTION_TOOLS and criterion_ids):
                self._record_execution_evidence(role, name, arguments, result, criterion_ids, evidence_kind)
            if name in WRITE_TOOLS and result.ok and result.meta.get("path"):
                self.changed_files.add(str(result.meta["path"]))
            after = await self._run_hooks("after_tool", role)
            if after is not None and not after.ok:
                return after
            if name in WRITE_TOOLS and result.ok:
                after_write = await self._run_hooks("after_write", role)
                if after_write is not None and not after_write.ok:
                    return after_write
            return result
        except (TypeError, ValueError):
            return ToolResult(False, "invalid_tool_arguments", "", {})

    def _resolve_evidence_binding(self, name: str, arguments: dict[str, Any]) -> tuple[tuple[str, ...], str, str]:
        raw_ids = arguments.get("criterion_ids", [])
        if raw_ids is None:
            raw_ids = []
        if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
            return (), "", "invalid_criterion_ids"
        criterion_ids = tuple(dict.fromkeys(item.strip().upper() for item in raw_ids if item.strip()))
        if any(item not in self.criteria for item in criterion_ids):
            return criterion_ids, "", "unknown_criterion_id"

        if name in WRITE_TOOLS:
            evidence_kind = "write"
            eligible = [item.id for item in self.criteria.values() if item.kind == "file_change"]
        elif name == "run_command":
            requested_kind = str(arguments.get("evidence_kind") or "").strip()
            kind_map = {"verification": "file_change", "action": "command", "inspection": "inspection"}
            if requested_kind and requested_kind not in kind_map:
                return criterion_ids, requested_kind, "invalid_evidence_kind"
            if not requested_kind and criterion_ids:
                kinds = {self.criteria[item].kind for item in criterion_ids}
                reverse = {"file_change": "verification", "command": "action", "inspection": "inspection"}
                if any(item not in reverse for item in kinds):
                    return criterion_ids, "", "evidence_kind_required"
                requested_kind = reverse[next(iter(kinds))] if len(kinds) == 1 else "multi"
            if not requested_kind:
                actionable = [item for item in self.criteria.values() if item.kind in kind_map.values()]
                if len(actionable) == 1:
                    requested_kind = {"file_change": "verification", "command": "action", "inspection": "inspection"}[actionable[0].kind]
            evidence_kind = requested_kind or "verification"
            # A single command may both verify a file change and be the command the user asked to run.
            # Criterion type determines each ledger record's evidence kind.
            eligible = [item.id for item in self.criteria.values() if item.kind in kind_map.values()]
        elif name in INSPECTION_TOOLS:
            evidence_kind = "inspection"
            eligible = [item.id for item in self.criteria.values() if item.kind == "inspection"]
        else:
            return criterion_ids, "tool", ""

        if not criterion_ids and len(eligible) == 1:
            criterion_ids = (eligible[0],)
        if not criterion_ids and len(eligible) > 1 and name in WRITE_TOOLS:
            return (), evidence_kind, "criterion_ids_required"
        if any(item not in eligible for item in criterion_ids):
            return criterion_ids, evidence_kind, "criterion_kind_mismatch"
        return criterion_ids, evidence_kind, ""

    def _record_response_evidence(self, role: AgentRole, summary: str) -> None:
        criterion_ids = tuple(item.id for item in self.criteria.values() if item.kind == "response")
        if not criterion_ids:
            return
        self.evidence_ledger.append(EvidenceRecord(
            sequence=len(self.evidence_ledger) + 1,
            kind="response",
            criterion_ids=criterion_ids,
            ok=bool(summary.strip()),
            tool="assistant_summary",
            write_version=self.write_version,
            detail={"role": role.value, "summary": summary[:1200]},
        ))

    def _record_execution_evidence(self, role: AgentRole, name: str, arguments: dict[str, Any], result: ToolResult, criterion_ids: tuple[str, ...] = (), evidence_kind: str = "") -> None:
        if name in WRITE_TOOLS and result.ok:
            self.write_version += 1
        self.execution_evidence.append((name, result.ok))
        detail = {
            "tool": name,
            "role": role.value,
            "ok": result.ok,
            "code": result.code,
            "path": result.meta.get("path", arguments.get("path", "")),
            "command": arguments.get("command", ""),
            "exit_code": result.meta.get("exit_code"),
            "output": result.content,
            "diff": result.meta.get("diff", ""),
            "criterion_ids": list(criterion_ids),
            "evidence_kind": evidence_kind,
            "write_version": self.write_version,
        }
        self.evidence_details.append(detail)
        record_detail = {
            "role": role.value,
            "code": result.code,
            "path": result.meta.get("path", arguments.get("path", "")),
            "command": arguments.get("command", ""),
            "exit_code": result.meta.get("exit_code"),
            "output": result.content[-2000:],
            "diff": str(result.meta.get("diff", ""))[:2000],
        }
        if name == "run_command" and criterion_ids:
            kind_for_criterion = {"file_change": "verification", "command": "action", "inspection": "inspection"}
            grouped: dict[str, list[str]] = {}
            for criterion_id in criterion_ids:
                ledger_kind = kind_for_criterion[self.criteria[criterion_id].kind]
                grouped.setdefault(ledger_kind, []).append(criterion_id)
            ledger_bindings = [(kind, tuple(ids)) for kind, ids in grouped.items()]
        else:
            ledger_kind = "write" if name in WRITE_TOOLS else evidence_kind or ("inspection" if name in INSPECTION_TOOLS else "tool")
            ledger_bindings = [(ledger_kind, criterion_ids)]
        for ledger_kind, bound_ids in ledger_bindings:
            self.evidence_ledger.append(EvidenceRecord(
                sequence=len(self.evidence_ledger) + 1,
                kind=ledger_kind,
                criterion_ids=bound_ids,
                ok=result.ok,
                tool=name,
                write_version=self.write_version,
                detail=record_detail,
            ))

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
