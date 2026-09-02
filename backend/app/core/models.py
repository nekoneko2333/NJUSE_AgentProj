from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from pydantic import BaseModel, Field


class AgentRole(StrEnum):
    SINGLE = "single"
    PLANNER = "planner"
    EXPLORER = "explorer"
    CODER = "coder"
    REVIEWER = "reviewer"


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    AGENT_STARTED = "agent_started"
    AGENT_FINISHED = "agent_finished"
    TOOL_REQUESTED = "tool_requested"
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TASK_FINISHED = "task_finished"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_RESTORED = "checkpoint_restored"


class AgentEvent(BaseModel):
    type: EventType
    session_id: str
    turn_id: str | None = None
    role: AgentRole | None = None
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateTaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    workspace: str = Field(min_length=1, max_length=1024)
    locale: str = "zh-CN"
    command_mode: Literal["auto", "ask", "deny"] = "auto"
    cross_session_memory_enabled: bool = False
    agent_mode: Literal["multi", "single", "adaptive"] = "multi"
    agent_config: dict[str, Any] = Field(default_factory=dict)


class AppendTurnRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    locale: str = "zh-CN"


class UpdateCommandModeRequest(BaseModel):
    command_mode: Literal["auto", "ask", "deny"]


class UpdateCrossSessionMemoryRequest(BaseModel):
    enabled: bool


class UpdateAgentWorkflowRequest(BaseModel):
    agent_mode: Literal["multi", "single", "adaptive"]
    agent_config: dict[str, Any] = Field(default_factory=dict)


class ProjectConfigWriteRequest(BaseModel):
    kind: Literal["agents", "hooks", "mcp"]
    content: str = Field(max_length=100_000)
    expected_sha256: str = Field(default="", max_length=64)


class ApprovalDecisionRequest(BaseModel):
    allow: bool


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=500_000)
    expected_sha256: str = Field(min_length=64, max_length=64)


class ModelSettingsRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    context_budget_chars: int = Field(ge=2000, le=200_000)
    max_turns: int = Field(ge=1, le=50)
    command_timeout_seconds: int = Field(ge=1, le=600)


class ExecutionSnapshot(BaseModel):
    id: str
    session_id: str
    turn_id: str | None = None
    status: Literal["queued", "running", "waiting_approval", "cancel_requested", "cancelled", "succeeded", "failed", "interrupted"]
    reason: str = ""
    created_at: str
    updated_at: str


class CheckpointSnapshot(BaseModel):
    id: str
    session_id: str
    turn_id: str | None = None
    label: str
    status: Literal["available", "restored", "conflicted"] = "available"
    files: list[str] = Field(default_factory=list)
    created_at: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class ConversationTurn(BaseModel):
    id: str
    session_id: str
    position: int
    user_content: str
    assistant_summary: str = ""
    status: str
    created_at: str


class SessionListItem(BaseModel):
    id: str
    title: str
    task: str
    workspace: str
    locale: str
    status: str
    updated_at: str
    turn_count: int
    command_mode: Literal["auto", "ask", "deny"] = "auto"
    cross_session_memory_enabled: bool = False
    agent_mode: Literal["multi", "single", "adaptive"] = "multi"


class SessionSnapshot(BaseModel):
    id: str
    title: str = ""
    task: str
    workspace: str
    locale: str = "zh-CN"
    status: str
    memory_summary: str = ""
    created_at: str = ""
    updated_at: str = ""
    turns: list[ConversationTurn] = Field(default_factory=list)
    events: list[AgentEvent]
    command_mode: Literal["auto", "ask", "deny"] = "auto"
    cross_session_memory_enabled: bool = False
    agent_mode: Literal["multi", "single", "adaptive"] = "multi"
    agent_config: dict[str, Any] = Field(default_factory=dict)
