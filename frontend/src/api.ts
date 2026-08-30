export type AgentEvent = { type: string; session_id: string; role?: 'planner' | 'explorer' | 'coder' | 'reviewer'; summary: string; payload: Record<string, unknown> }
export type Session = { id: string; task: string; workspace: string; status: string; events: AgentEvent[] }
const API = 'http://localhost:8000/api'
export async function createSession(task: string, workspace: string, locale: string): Promise<Session> { const res = await fetch(`${API}/sessions`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task, workspace, locale}) }); if (!res.ok) throw new Error(await res.text()); return res.json() }
export async function demoRun(id: string) { const res = await fetch(`${API}/sessions/${id}/demo-run`, {method:'POST'}); if (!res.ok) throw new Error(await res.text()) }
export async function runAgent(id: string) { const res = await fetch(`${API}/sessions/${id}/run`, {method:'POST'}); if (!res.ok) throw new Error(await res.text()); const body = await res.json(); if (body.status === 'failed') throw new Error(body.reason ?? 'agent_failed') }
export function subscribe(id: string, onEvent: (event: AgentEvent) => void) { const stream = new EventSource(`${API}/sessions/${id}/events`); stream.onmessage = (message) => onEvent(JSON.parse(message.data)); return () => stream.close() }
