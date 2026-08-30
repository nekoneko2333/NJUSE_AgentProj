from __future__ import annotations

from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field


class AgentRole(StrEnum):
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
    TASK_FINISHED = "task_finished"
    TASK_FAILED = "task_failed"


class AgentEvent(BaseModel):
    type: EventType
    session_id: str
    role: AgentRole | None = None
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateTaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    workspace: str = Field(min_length=1, max_length=1024)
    locale: str = "zh-CN"


class SessionSnapshot(BaseModel):
    id: str
    task: str
    workspace: str
    status: str
    events: list[AgentEvent]
