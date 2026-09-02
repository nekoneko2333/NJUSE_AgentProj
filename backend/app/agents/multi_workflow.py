from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from app.core.models import AgentRole


class MultiStage(str, Enum):
    """Small, explicit graph for the multi-agent execution path."""

    INIT = "init"
    PLAN = "plan"
    EXPLORE = "explore"
    IMPLEMENT = "implement"
    REVIEW = "review"
    REPAIR = "repair"
    FINALIZE = "finalize"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class MultiAgentLimits:
    """Hard orchestration budgets; these are not prompt-controlled settings."""

    planner_turns: int = 1
    explorer_turns: int = 4
    explorer_tool_calls: int = 4
    coder_turns: int = 8
    coder_tool_calls: int = 10
    coder_inspection_calls: int = 4
    reviewer_calls: int = 1
    repair_cycles: int = 1
    repair_turns: int = 5
    repair_tool_calls: int = 6
    repair_inspection_calls: int = 3
    handoff_chars: int = 3000
    evidence_chars: int = 4000
    max_node_steps: int = 7

    def role_turns(self, role: AgentRole, *, repairing: bool = False) -> int:
        if repairing:
            return self.repair_turns
        return {
            AgentRole.PLANNER: self.planner_turns,
            AgentRole.EXPLORER: self.explorer_turns,
            AgentRole.CODER: self.coder_turns,
            AgentRole.REVIEWER: self.reviewer_calls,
        }[role]

    def role_tool_calls(self, role: AgentRole, *, repairing: bool = False) -> int:
        if repairing:
            return self.repair_tool_calls
        return {
            AgentRole.PLANNER: 0,
            AgentRole.EXPLORER: self.explorer_tool_calls,
            AgentRole.CODER: self.coder_tool_calls,
            AgentRole.REVIEWER: 0,
        }[role]

    def public_dict(self) -> dict[str, int]:
        return asdict(self)


DEFAULT_MULTI_LIMITS = MultiAgentLimits()


_ALLOWED_TRANSITIONS: dict[MultiStage, set[MultiStage]] = {
    MultiStage.INIT: {MultiStage.PLAN, MultiStage.FAILED},
    MultiStage.PLAN: {MultiStage.EXPLORE, MultiStage.FAILED},
    MultiStage.EXPLORE: {MultiStage.IMPLEMENT, MultiStage.FAILED},
    MultiStage.IMPLEMENT: {MultiStage.REVIEW, MultiStage.FAILED},
    MultiStage.REVIEW: {MultiStage.REPAIR, MultiStage.FINALIZE, MultiStage.FAILED},
    # The second REVIEW edge is retained only for compatibility with injected
    # legacy reviewers. Production structured review closes repair by evidence.
    MultiStage.REPAIR: {MultiStage.REVIEW, MultiStage.FINALIZE, MultiStage.FAILED},
    MultiStage.FINALIZE: {MultiStage.DONE, MultiStage.FAILED},
    MultiStage.DONE: set(),
    MultiStage.FAILED: set(),
}


@dataclass
class MultiWorkflowState:
    stage: MultiStage = MultiStage.INIT
    step: int = 0
    limit: int = DEFAULT_MULTI_LIMITS.max_node_steps

    def transition(self, target: MultiStage) -> None:
        if target not in _ALLOWED_TRANSITIONS[self.stage]:
            raise RuntimeError(f"invalid_multi_workflow_transition:{self.stage.value}->{target.value}")
        if target not in {MultiStage.DONE, MultiStage.FAILED}:
            if self.step >= self.limit:
                raise RuntimeError("multi_workflow_step_limit")
            self.step += 1
        self.stage = target

    def fail(self) -> None:
        if self.stage not in {MultiStage.DONE, MultiStage.FAILED}:
            self.stage = MultiStage.FAILED

    def payload(self) -> dict[str, int | str]:
        return {
            "workflow_state": self.stage.value,
            "workflow_step": self.step,
            "workflow_step_limit": self.limit,
        }
