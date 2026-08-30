from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.core.models import AgentEvent, AgentRole, CreateTaskRequest, EventType, SessionSnapshot
from app.core.settings import settings
from app.agents.orchestrator import Orchestrator
from app.llm.client import OpenAICompatibleClient, LLMError
from app.runtime.workspace import Workspace, WorkspaceError
from app.tools.registry import ToolRegistry

app = FastAPI(title="MossCode API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

sessions: dict[str, SessionSnapshot] = {}
subscribers: dict[str, list[asyncio.Queue[AgentEvent]]] = defaultdict(list)


async def publish(event: AgentEvent) -> None:
    sessions[event.session_id].events.append(event)
    for queue in subscribers[event.session_id]:
        await queue.put(event)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sessions", response_model=SessionSnapshot)
async def create_session(payload: CreateTaskRequest) -> SessionSnapshot:
    try:
        Workspace(payload.workspace)
    except WorkspaceError as error:
        raise HTTPException(422, detail=str(error)) from error
    session = SessionSnapshot(id=str(uuid4()), task=payload.task, workspace=payload.workspace, locale=payload.locale, status="created", events=[])
    sessions[session.id] = session
    await publish(AgentEvent(type=EventType.TASK_CREATED, session_id=session.id, summary="任务已创建", payload={"locale": payload.locale}))
    return session


@app.get("/api/sessions/{session_id}", response_model=SessionSnapshot)
async def get_session(session_id: str) -> SessionSnapshot:
    if session_id not in sessions:
        raise HTTPException(404, detail="session_not_found")
    return sessions[session_id]


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str) -> StreamingResponse:
    if session_id not in sessions:
        raise HTTPException(404, detail="session_not_found")

    async def event_stream():
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        subscribers[session_id].append(queue)
        try:
            for event in sessions[session_id].events:
                yield f"data: {event.model_dump_json()}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {event.model_dump_json()}\n\n"
        finally:
            subscribers[session_id].remove(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{session_id}/demo-run")
async def demo_run(session_id: str) -> dict[str, str]:
    """无需消耗模型额度的事件流联调，用于先验证 GUI。"""
    if session_id not in sessions:
        raise HTTPException(404, detail="session_not_found")
    session = sessions[session_id]
    session.status = "running"
    tools = ToolRegistry(Workspace(session.workspace))
    for role, summary in ((AgentRole.PLANNER, "正在拆分任务"), (AgentRole.EXPLORER, "正在检索工作区"), (AgentRole.CODER, "等待模型配置后实施修改"), (AgentRole.REVIEWER, "等待可审查的变更")):
        await publish(AgentEvent(type=EventType.AGENT_STARTED, session_id=session_id, role=role, summary=summary))
        if role == AgentRole.EXPLORER:
            result = tools.list_files()
            await publish(AgentEvent(type=EventType.TOOL_FINISHED, session_id=session_id, role=role, summary="已读取工作区目录", payload={"tool": "list_files", "result": result.meta}))
        await publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=session_id, role=role, summary="本阶段完成"))
    session.status = "waiting_model"
    await publish(AgentEvent(type=EventType.TASK_FINISHED, session_id=session_id, summary="演示编排完成；配置模型后可执行真实任务。"))
    return {"status": session.status}


@app.post("/api/sessions/{session_id}/run")
async def run_agent(session_id: str) -> dict[str, str]:
    """真实多智能体运行入口，仅在用户从 GUI 主动触发时调用。"""
    if session_id not in sessions:
        raise HTTPException(404, detail="session_not_found")
    session = sessions[session_id]
    if session.status == "running":
        raise HTTPException(409, detail="session_already_running")
    session.status = "running"
    client = OpenAICompatibleClient(api_key=settings.api_key, base_url=settings.base_url, model=settings.model)
    runner = Orchestrator(session_id, session.task, ToolRegistry(Workspace(session.workspace), settings.command_timeout_seconds), client, settings, publish, session.locale)
    try:
        await runner.run()
    except LLMError as error:
        session.status = "failed"
        return {"status": session.status, "reason": str(error)}
    session.status = "finished"
    return {"status": session.status}
