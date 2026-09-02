from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.core.context import ContextManager
from app.core.auth import LocalAuth
from app.core.checkpoints import CheckpointManager, content_sha256
from app.core.extensions import HookConfig, MCPManager, load_project_rules
from app.core.models import AgentEvent, AgentRole, AppendTurnRequest, ApprovalDecisionRequest, CheckpointSnapshot, CreateTaskRequest, EventType, ExecutionSnapshot, FileWriteRequest, LoginRequest, ModelSettingsRequest, ProjectConfigWriteRequest, SessionListItem, SessionSnapshot, UpdateAgentWorkflowRequest, UpdateCommandModeRequest, UpdateCrossSessionMemoryRequest
from app.core.settings import settings
from app.agents.orchestrator import ORCHESTRATOR_PROTOCOL, Orchestrator, is_continuation_task, normalize_agent_config
from app.llm.client import OpenAICompatibleClient, LLMError
from app.runtime.workspace import Workspace, WorkspaceError
from app.storage.sqlite_store import SQLiteStore
from app.tools.registry import ToolRegistry

app = FastAPI(title="MossCode API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

store = SQLiteStore(settings.database_path)
auth = LocalAuth(settings)
subscribers: dict[str, list[asyncio.Queue[AgentEvent]]] = defaultdict(list)
pending_approvals: dict[str, tuple[str, asyncio.Future[bool]]] = {}
execution_tasks: dict[str, asyncio.Task[None]] = {}
execution_cancellations: dict[str, threading.Event] = {}
checkpoint_root = Path(settings.database_path).expanduser().resolve().parent / "checkpoints"
runtime_model_settings = {
    "base_url": settings.base_url,
    "model": settings.model,
    "context_budget_chars": settings.context_budget_chars,
    "max_turns": settings.max_turns,
    "command_timeout_seconds": settings.command_timeout_seconds,
}


async def publish(event: AgentEvent) -> None:
    store.add_event(event)
    for queue in subscribers[event.session_id]:
        await queue.put(event)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "storage": "sqlite", "orchestrator_protocol": ORCHESTRATOR_PROTOCOL}


@app.get("/api/settings/model")
async def get_model_settings(_user: str = Depends(auth.require_user)) -> dict[str, object]:
    return {**runtime_model_settings, "api_key_configured": bool(settings.api_key)}


@app.put("/api/settings/model")
async def update_model_settings(payload: ModelSettingsRequest, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    runtime_model_settings.update(payload.model_dump())
    return {**runtime_model_settings, "api_key_configured": bool(settings.api_key)}


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, object]:
    user = auth.user_for(request)
    return {"authenticated": user is not None, "username": user or ""}


@app.post("/api/auth/login")
async def login(payload: LoginRequest, response: Response) -> dict[str, str]:
    token = auth.authenticate(payload.username, payload.password)
    if token is None:
        raise HTTPException(401, detail="invalid_credentials")
    auth.set_cookie(response, token)
    return {"username": payload.username}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    auth.logout(request, response)
    return {"status": "ok"}


@app.get("/api/sessions", response_model=list[SessionListItem])
async def list_sessions(_user: str = Depends(auth.require_user)) -> list[SessionListItem]:
    return store.list_sessions()


@app.post("/api/sessions", response_model=SessionSnapshot)
async def create_session(payload: CreateTaskRequest, _user: str = Depends(auth.require_user)) -> SessionSnapshot:
    try:
        Workspace(payload.workspace)
    except WorkspaceError as error:
        raise HTTPException(422, detail=str(error)) from error
    session, turn = store.create_session(task=payload.task, workspace=payload.workspace, locale=payload.locale, command_mode=payload.command_mode, cross_session_memory_enabled=payload.cross_session_memory_enabled, agent_mode=payload.agent_mode, agent_config=normalize_agent_config(payload.agent_config))
    await publish(AgentEvent(type=EventType.TASK_CREATED, session_id=session.id, turn_id=turn.id, summary="任务已创建", payload={"locale": payload.locale, "task": payload.task, "position": turn.position}))
    return store.get_session(session.id)  # type: ignore[return-value]


@app.get("/api/sessions/{session_id}", response_model=SessionSnapshot)
async def get_session(session_id: str, _user: str = Depends(auth.require_user)) -> SessionSnapshot:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    return session


@app.post("/api/sessions/{session_id}/turns", response_model=SessionSnapshot)
async def append_turn(session_id: str, payload: AppendTurnRequest, _user: str = Depends(auth.require_user)) -> SessionSnapshot:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    turn = store.append_turn(session_id, payload.task, payload.locale)
    if turn is None:
        raise HTTPException(404, detail="session_not_found")
    await publish(AgentEvent(type=EventType.TASK_CREATED, session_id=session_id, turn_id=turn.id, summary="已追加一轮任务", payload={"locale": payload.locale, "task": payload.task, "position": turn.position}))
    return store.get_session(session_id)  # type: ignore[return-value]


@app.patch("/api/sessions/{session_id}/command-mode", response_model=SessionSnapshot)
async def update_command_mode(session_id: str, payload: UpdateCommandModeRequest, _user: str = Depends(auth.require_user)) -> SessionSnapshot:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    if session.status == "running":
        raise HTTPException(409, detail="session_already_running")
    store.update_command_mode(session_id, payload.command_mode)
    return store.get_session(session_id)  # type: ignore[return-value]


@app.patch("/api/sessions/{session_id}/cross-session-memory", response_model=SessionSnapshot)
async def update_cross_session_memory(session_id: str, payload: UpdateCrossSessionMemoryRequest, _user: str = Depends(auth.require_user)) -> SessionSnapshot:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    if session.status == "running":
        raise HTTPException(409, detail="session_already_running")
    store.update_cross_session_memory(session_id, payload.enabled)
    return store.get_session(session_id)  # type: ignore[return-value]


@app.patch("/api/sessions/{session_id}/agent-workflow", response_model=SessionSnapshot)
async def update_agent_workflow(session_id: str, payload: UpdateAgentWorkflowRequest, _user: str = Depends(auth.require_user)) -> SessionSnapshot:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    if session.status == "running":
        raise HTTPException(409, detail="session_already_running")
    store.update_agent_workflow(session_id, payload.agent_mode, normalize_agent_config(payload.agent_config))
    return store.get_session(session_id)  # type: ignore[return-value]


@app.get("/api/sessions/{session_id}/workspace-files")
async def get_workspace_files(session_id: str, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    result = ToolRegistry(Workspace(session.workspace)).workspace_tree()
    if not result.ok:
        raise HTTPException(500, detail=result.code)
    return result.meta


def _config_file(workspace: Path, relative_path: str) -> dict[str, object]:
    target = (workspace / relative_path).resolve()
    if not target.is_file():
        return {"path": relative_path, "exists": False, "content": "", "sha256": ""}
    content = target.read_text(encoding="utf-8", errors="replace")
    return {"path": relative_path, "exists": True, "content": content, "sha256": content_sha256(content)}


def _project_config_payload(workspace: Path) -> dict[str, object]:
    rules = load_project_rules(workspace)
    return {
        "rules": {"text": rules.text, "sources": rules.sources, "truncated": rules.truncated, "file": _config_file(workspace, "AGENTS.md")},
        "hooks": {**HookConfig(workspace).status(), "file": _config_file(workspace, ".mosscode/hooks.json")},
        "mcp": {**MCPManager(workspace, min(int(runtime_model_settings["command_timeout_seconds"]), 15)).status(), "file": _config_file(workspace, ".mosscode/mcp.json")},
    }


@app.get("/api/sessions/{session_id}/project-config")
async def get_project_config(session_id: str, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    return _project_config_payload(Workspace(session.workspace).root)


@app.put("/api/sessions/{session_id}/project-config")
async def update_project_config(session_id: str, payload: ProjectConfigWriteRequest, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    if session.status == "running":
        raise HTTPException(409, detail="session_already_running")
    paths = {"agents": "AGENTS.md", "hooks": ".mosscode/hooks.json", "mcp": ".mosscode/mcp.json"}
    path = paths[payload.kind]
    if payload.kind in {"hooks", "mcp"}:
        try:
            parsed = json.loads(payload.content)
        except json.JSONDecodeError as error:
            raise HTTPException(422, detail="invalid_config_json") from error
        if not isinstance(parsed, dict) or (payload.kind == "hooks" and not isinstance(parsed.get("hooks", {}), dict)) or (payload.kind == "mcp" and not isinstance(parsed.get("servers", {}), dict)):
            raise HTTPException(422, detail="invalid_config_shape")
    workspace = Workspace(session.workspace)
    current = _config_file(workspace.root, path)
    if str(current["sha256"]) != payload.expected_sha256:
        raise HTTPException(409, detail="file_changed")
    checkpoint = CheckpointManager(checkpoint_root, store, session_id, None, f"更新项目配置 {path}")
    result = ToolRegistry(workspace, checkpoint=checkpoint).write_file(path, payload.content)
    if not result.ok:
        raise HTTPException(422, detail=result.code)
    await publish(AgentEvent(type=EventType.CHECKPOINT_CREATED, session_id=session_id, summary="已更新项目配置并创建检查点。", payload={"checkpoint_id": result.meta.get("checkpoint_id"), "path": path}))
    return _project_config_payload(workspace.root)


@app.get("/api/sessions/{session_id}/files/content")
async def get_file_content(session_id: str, path: str, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    result = ToolRegistry(Workspace(session.workspace)).read_file_content(path)
    if not result.ok:
        raise HTTPException(413 if result.code == "file_too_large" else 404, detail=result.code)
    return {"content": result.content, **result.meta}


@app.get("/api/sessions/{session_id}/search")
async def search_workspace(session_id: str, q: str, path: str = ".", _user: str = Depends(auth.require_user)) -> dict[str, object]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    result = ToolRegistry(Workspace(session.workspace)).search_text(q, path)
    if not result.ok:
        raise HTTPException(422, detail=result.code)
    return result.meta


@app.put("/api/sessions/{session_id}/files/content")
async def update_file_content(session_id: str, payload: FileWriteRequest, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    workspace = Workspace(session.workspace)
    tools = ToolRegistry(workspace)
    current = tools.read_file_content(payload.path)
    if not current.ok:
        raise HTTPException(404, detail=current.code)
    if current.meta.get("sha256") != payload.expected_sha256:
        raise HTTPException(409, detail="file_changed")
    manager = CheckpointManager(checkpoint_root, store, session_id, None, f"用户编辑 {payload.path}")
    result = ToolRegistry(workspace, checkpoint=manager).write_file(payload.path, payload.content)
    if not result.ok:
        raise HTTPException(422, detail=result.code)
    await publish(AgentEvent(type=EventType.CHECKPOINT_CREATED, session_id=session_id, summary="已为用户编辑创建检查点。", payload={"checkpoint_id": result.meta.get("checkpoint_id"), "path": payload.path}))
    return result.meta


@app.get("/api/sessions/{session_id}/checkpoints", response_model=list[CheckpointSnapshot])
async def list_checkpoints(session_id: str, _user: str = Depends(auth.require_user)) -> list[CheckpointSnapshot]:
    if store.get_session(session_id) is None:
        raise HTTPException(404, detail="session_not_found")
    return store.list_checkpoints(session_id)


@app.post("/api/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(checkpoint_id: str, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    checkpoint = store.get_checkpoint(checkpoint_id)
    if checkpoint is None:
        raise HTTPException(404, detail="checkpoint_not_found")
    session = store.get_session(checkpoint.session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    ok, code, files = CheckpointManager.restore(checkpoint_root, store, checkpoint_id, Workspace(session.workspace).root)
    if not ok:
        raise HTTPException(409 if code == "checkpoint_conflict" else 404, detail={"code": code, "files": files})
    await publish(AgentEvent(type=EventType.CHECKPOINT_RESTORED, session_id=session.id, turn_id=checkpoint.turn_id, summary="检查点已恢复。", payload={"checkpoint_id": checkpoint_id, "files": files}))
    return {"status": "restored", "files": files}


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str, _user: str = Depends(auth.require_user)) -> StreamingResponse:
    if store.get_session(session_id) is None:
        raise HTTPException(404, detail="session_not_found")

    async def event_stream():
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        subscribers[session_id].append(queue)
        try:
            for event in store.list_events(session_id):
                yield f"data: {event.model_dump_json()}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {event.model_dump_json()}\n\n"
        finally:
            subscribers[session_id].remove(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{session_id}/demo-run")
async def demo_run(session_id: str, _user: str = Depends(auth.require_user)) -> dict[str, str]:
    """无需消耗模型额度的事件流联调，用于先验证 GUI。"""
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    turn = store.pending_turn(session_id)
    if turn is None:
        raise HTTPException(409, detail="no_pending_turn")
    store.update_session_status(session_id, "running")
    store.update_turn(turn.id, status="running")
    tools = ToolRegistry(Workspace(session.workspace))
    for role, summary in ((AgentRole.PLANNER, "正在拆分任务"), (AgentRole.EXPLORER, "正在检索工作区"), (AgentRole.CODER, "等待模型配置后实施修改"), (AgentRole.REVIEWER, "等待可审查的变更")):
        await publish(AgentEvent(type=EventType.AGENT_STARTED, session_id=session_id, turn_id=turn.id, role=role, summary=summary))
        if role == AgentRole.EXPLORER:
            result = tools.list_files()
            await publish(AgentEvent(type=EventType.TOOL_FINISHED, session_id=session_id, turn_id=turn.id, role=role, summary="已读取工作区目录", payload={"tool": "list_files", "result": result.meta}))
        await publish(AgentEvent(type=EventType.AGENT_FINISHED, session_id=session_id, turn_id=turn.id, role=role, summary="本阶段完成"))
    await publish(AgentEvent(type=EventType.TASK_FINISHED, session_id=session_id, turn_id=turn.id, summary="演示编排完成；配置模型后可执行真实任务。"))
    store.update_turn(turn.id, status="finished", assistant_summary="演示编排已完成。")
    return {"status": "finished"}


def _start_execution(session_id: str) -> ExecutionSnapshot:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    if session.status == "running":
        raise HTTPException(409, detail="session_already_running")
    turn = store.pending_turn(session_id)
    if turn is None:
        raise HTTPException(409, detail="no_pending_turn")
    execution = store.create_execution(session_id, turn.id)
    cancel_event = threading.Event()
    execution_cancellations[execution.id] = cancel_event
    task = asyncio.create_task(_process_execution(execution.id, session_id, cancel_event))
    execution_tasks[execution.id] = task
    task.add_done_callback(lambda _task: (execution_tasks.pop(execution.id, None), execution_cancellations.pop(execution.id, None)))
    return execution


@app.post("/api/sessions/{session_id}/executions", response_model=ExecutionSnapshot, status_code=202)
async def start_execution(session_id: str, _user: str = Depends(auth.require_user)) -> ExecutionSnapshot:
    return _start_execution(session_id)


@app.post("/api/sessions/{session_id}/run", response_model=ExecutionSnapshot, status_code=202)
async def run_agent(session_id: str, _user: str = Depends(auth.require_user)) -> ExecutionSnapshot:
    """兼容旧客户端的后台执行入口。"""
    return _start_execution(session_id)


@app.get("/api/executions/{execution_id}", response_model=ExecutionSnapshot)
async def get_execution(execution_id: str, _user: str = Depends(auth.require_user)) -> ExecutionSnapshot:
    try:
        return store.get_execution(execution_id)
    except KeyError as error:
        raise HTTPException(404, detail="execution_not_found") from error


@app.get("/api/sessions/{session_id}/executions/latest", response_model=ExecutionSnapshot | None)
async def get_latest_execution(session_id: str, _user: str = Depends(auth.require_user)) -> ExecutionSnapshot | None:
    if store.get_session(session_id) is None:
        raise HTTPException(404, detail="session_not_found")
    return store.latest_execution(session_id)


@app.post("/api/executions/{execution_id}/cancel", response_model=ExecutionSnapshot)
async def cancel_execution(execution_id: str, _user: str = Depends(auth.require_user)) -> ExecutionSnapshot:
    try:
        execution = store.get_execution(execution_id)
    except KeyError as error:
        raise HTTPException(404, detail="execution_not_found") from error
    if execution.status not in {"queued", "running", "waiting_approval"}:
        return execution
    store.update_execution(execution_id, "cancel_requested")
    cancel_event = execution_cancellations.get(execution_id)
    if cancel_event:
        cancel_event.set()
    approval_futures = [future for owner, future in pending_approvals.values() if owner == execution_id]
    for future in approval_futures:
        if not future.done():
            future.set_result(False)
    return store.get_execution(execution_id)


@app.post("/api/turns/{turn_id}/retry", response_model=ExecutionSnapshot, status_code=202)
async def retry_turn(turn_id: str, _user: str = Depends(auth.require_user)) -> ExecutionSnapshot:
    try:
        turn = store.retry_turn(turn_id)
    except KeyError as error:
        raise HTTPException(404, detail="turn_not_found") from error
    except ValueError as error:
        raise HTTPException(409, detail=str(error)) from error
    return _start_execution(turn.session_id)


async def _process_execution(execution_id: str, session_id: str, cancel_event: threading.Event) -> None:
    store.update_execution(execution_id, "running")
    processed = 0
    while turn := store.pending_turn(session_id):
        if cancel_event.is_set():
            await publish(AgentEvent(type=EventType.TASK_CANCELLED, session_id=session_id, turn_id=turn.id, summary="本轮任务已由用户取消。", payload={"reason": "cancelled_by_user"}))
            store.update_turn(turn.id, status="cancelled", assistant_summary="cancelled_by_user")
            store.update_execution(execution_id, "cancelled", "cancelled_by_user")
            return
        session = store.get_session(session_id)
        if session is None:
            store.update_execution(execution_id, "failed", "session_not_found")
            return
        store.update_session_status(session_id, "running")
        store.update_turn(turn.id, status="running")
        effective_settings = replace(settings, **runtime_model_settings)
        context_manager = ContextManager(
            budget_chars=effective_settings.context_budget_chars,
            recent_turns=settings.context_recent_turns,
            summary_chars=settings.context_summary_chars,
        )
        # 排队中的后续指令不能提前泄漏给当前轮；只构建截至当前轮的上下文。
        context_turns = [item for item in store.list_turns(session_id) if item.position <= turn.position]
        prior_turns = [item for item in context_turns if item.position < turn.position]
        reference_turn = next(
            (item for item in reversed(prior_turns) if item.status in {"finished", "failed", "cancelled", "interrupted"} and not is_continuation_task(item.user_content)),
            None,
        )
        previous_attempt = any(
            event.turn_id == turn.id and event.type in {EventType.TASK_FAILED, EventType.TASK_CANCELLED, EventType.TASK_FINISHED}
            for event in store.list_events(session_id)
        )
        shared_memory = store.workspace_memory(session.workspace, session_id) if session.cross_session_memory_enabled else ""
        shared_preferences = store.workspace_preferences(session.workspace, session_id) if session.cross_session_memory_enabled else []
        context = context_manager.build(context_turns, shared_memory=shared_memory, shared_preferences="\n".join(f"- {item}" for item in shared_preferences))
        store.set_memory_summary(session_id, context.memory_summary)
        client = OpenAICompatibleClient(api_key=effective_settings.api_key, base_url=effective_settings.base_url, model=effective_settings.model)

        async def publish_turn(event: AgentEvent) -> None:
            await publish(event.model_copy(update={"turn_id": turn.id}))

        async def request_command_approval(role: AgentRole, command: str, cwd: str) -> bool:
            approval_id = str(uuid4())
            future = asyncio.get_running_loop().create_future()
            pending_approvals[approval_id] = (execution_id, future)
            store.update_execution(execution_id, "waiting_approval")
            await publish_turn(AgentEvent(
                type=EventType.APPROVAL_REQUESTED,
                session_id=session_id,
                role=role,
                summary="命令正在等待用户授权。",
                payload={"approval_id": approval_id, "command": command, "cwd": cwd},
            ))
            try:
                allowed = await asyncio.wait_for(future, timeout=settings.approval_timeout_seconds)
            except asyncio.TimeoutError:
                allowed = False
            finally:
                pending_approvals.pop(approval_id, None)
                if not cancel_event.is_set():
                    store.update_execution(execution_id, "running")
            await publish_turn(AgentEvent(
                type=EventType.APPROVAL_RESOLVED,
                session_id=session_id,
                role=role,
                summary="命令已允许。" if allowed else "命令未获允许。",
                payload={"approval_id": approval_id, "allowed": allowed},
            ))
            return allowed

        checkpoint = CheckpointManager(checkpoint_root, store, session_id, turn.id, f"第 {turn.position} 轮 Agent 修改")
        runner = Orchestrator(
            session_id,
            turn.user_content,
            ToolRegistry(Workspace(session.workspace), effective_settings.command_timeout_seconds, checkpoint=checkpoint, cancel_event=cancel_event),
            client,
            effective_settings,
            publish_turn,
            session.locale,
            context.text,
            context_manager,
            command_mode=session.command_mode,
            request_command_approval=request_command_approval,
            cancelled=cancel_event.is_set,
            project_rules=load_project_rules(Workspace(session.workspace).root, min(6000, effective_settings.context_budget_chars // 2)).text,
            execution_mode=session.agent_mode,
            agent_config=session.agent_config,
            memory_metadata={"cross_session_enabled": session.cross_session_memory_enabled, "shared_memory_loaded": bool(shared_memory), "preference_count": len(shared_preferences)},
            reference_task=reference_turn.user_content if reference_turn else "",
            resume_existing=previous_attempt or (is_continuation_task(turn.user_content) and reference_turn is not None),
        )
        try:
            completed = await runner.run()
        except asyncio.CancelledError:
            await publish_turn(AgentEvent(type=EventType.TASK_CANCELLED, session_id=session_id, summary="本轮任务已由用户取消。", payload={"reason": "cancelled_by_user"}))
            store.update_turn(turn.id, status="cancelled", assistant_summary="cancelled_by_user")
            store.update_execution(execution_id, "cancelled", "cancelled_by_user")
            return
        except LLMError as error:
            store.update_turn(turn.id, status="failed", assistant_summary=str(error))
            store.update_execution(execution_id, "failed", str(error))
            return
        except Exception:
            await publish_turn(AgentEvent(type=EventType.TASK_FAILED, session_id=session_id, summary="任务执行发生内部错误。", payload={"reason": "internal_error"}))
            store.update_turn(turn.id, status="failed", assistant_summary="internal_error")
            store.update_execution(execution_id, "failed", "internal_error")
            return
        turn_events = [event for event in store.list_events(session_id) if event.turn_id == turn.id]
        final_events = [event for event in turn_events if event.type in (EventType.TASK_FINISHED, EventType.TASK_FAILED)]
        assistant_summary = final_events[-1].summary[-3000:] if final_events else "本轮任务已结束。"
        final_status = "finished" if completed else "failed"
        store.update_turn(turn.id, status=final_status, assistant_summary=assistant_summary)
        processed += 1
        if not completed:
            store.update_execution(execution_id, "failed", runner.failure_reason or "task_incomplete")
            return
    store.update_execution(execution_id, "succeeded")


@app.post("/api/approvals/{approval_id}")
async def resolve_approval(approval_id: str, payload: ApprovalDecisionRequest, _user: str = Depends(auth.require_user)) -> dict[str, object]:
    pending = pending_approvals.get(approval_id)
    if pending is None or pending[1].done():
        raise HTTPException(404, detail="approval_not_found")
    future = pending[1]
    future.set_result(payload.allow)
    return {"status": "resolved", "allowed": payload.allow}
