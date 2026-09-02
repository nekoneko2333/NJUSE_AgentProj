import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App, { selectDisplayEvents } from '../App'

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))

describe('App conversation stability', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mosscode.lastSession', 'session-1')
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/status')) return json({ authenticated: true, username: 'moss' })
      if (url.endsWith('/sessions')) return json([{ id:'session-1', title:'测试', task:'原任务', workspace:'C:\\workspace', locale:'zh-CN', status:'finished', updated_at:'now', turn_count:1, command_mode:'auto', cross_session_memory_enabled:false, agent_mode:'multi' }])
      if (url.endsWith('/sessions/session-1')) return json({ id:'session-1', title:'测试', task:'追加任务', workspace:'C:\\workspace', locale:'zh-CN', status:'finished', memory_summary:'', created_at:'now', updated_at:'now', command_mode:'auto', cross_session_memory_enabled:false, agent_mode:'multi', agent_config:{}, turns:[], events:[{ type:'task_created', session_id:'session-1', turn_id:'turn-1', summary:'', payload:{ task:'原任务', position:1 } }, { type:'task_finished', session_id:'session-1', turn_id:'turn-1', summary:'最终答复', payload:{} }, { type:'task_created', session_id:'session-1', turn_id:'turn-2', summary:'', payload:{ task:'追加任务', position:2 } }, { type:'task_finished', session_id:'session-1', turn_id:'turn-2', summary:'第二轮答复', payload:{} }] })
      if (url.endsWith('/sessions/session-1/command-mode')) return json({ id:'session-1', title:'测试', task:'追加任务', workspace:'C:\\workspace', locale:'zh-CN', status:'finished', memory_summary:'', created_at:'now', updated_at:'now', command_mode:'ask', turns:[], events:[] })
      if (url.endsWith('/sessions/session-1/cross-session-memory')) return json({ cross_session_memory_enabled:true })
      if (url.endsWith('/sessions/session-1/agent-workflow')) return json({ agent_mode:'single' })
      if (url.endsWith('/settings/model')) return json({ base_url:'https://example.test', model:'demo', context_budget_chars:12000, max_turns:12, command_timeout_seconds:30, api_key_configured:true })
      if (url.endsWith('/sessions/session-1/project-config')) return json({ rules:{text:'',sources:[],truncated:false,file:{path:'AGENTS.md',exists:false,content:'',sha256:''}}, hooks:{configured:false,enabled:false,events:{},file:{path:'.mosscode/hooks.json',exists:false,content:'',sha256:''}}, mcp:{configured:false,servers:[],tools:[],file:{path:'.mosscode/mcp.json',exists:false,content:'',sha256:''}} })
      if (url.includes('/workspace-files')) return json({ items:[] })
      throw new Error(`unexpected fetch ${url}`)
    }))
  })
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('keeps an existing history message mounted while typing', async () => {
    render(<App/>)
    await screen.findByText('最终答复')
    const message = document.querySelector('.final-response')
    expect(message).toBeTruthy()
    const editor = screen.getByPlaceholderText(/继续追问/)
    await userEvent.type(editor, '补充要求')
    await waitFor(() => expect(editor).toHaveValue('补充要求'))
    expect(document.querySelector('.final-response')).toBe(message)
    const navigator = await screen.findByRole('navigation', { name:'我的发言导航' })
    expect(within(navigator).getAllByRole('button')).toHaveLength(2)
    await userEvent.click(within(navigator).getByRole('button', { name:'跳转到第 2 轮发言' }))
  })

  it('uses a styled accessible menu instead of a native select', async () => {
    render(<App/>)
    const trigger = await screen.findByRole('button', { name:'终端权限：自动执行安全命令' })
    expect(document.querySelector('.permission-field select')).not.toBeInTheDocument()
    await userEvent.click(trigger)
    const listbox = screen.getByRole('listbox', { name:'终端权限' })
    await userEvent.click(within(listbox).getByRole('option', { name:/每次执行前询问/ }))
    await waitFor(() => expect(screen.getByRole('button', { name:'终端权限：每次执行前询问' })).toBeInTheDocument())
  })

  it('keeps memory in one settings location and exposes a real single-agent baseline', async () => {
    render(<App/>)
    await screen.findByText('最终答复')
    const inspectorTabs = document.querySelector('.tabs') as HTMLElement
    expect(within(inspectorTabs).queryByRole('button', { name:'设置' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name:'设置' }))
    await screen.findByText('Agent 编排与对比')
    expect(screen.getByRole('dialog', { name:'设置' })).toBeInTheDocument()
    expect(document.querySelector('.conversation')).toBeInTheDocument()
    expect(screen.getAllByText('跨对话项目记忆')).toHaveLength(1)
    await userEvent.click(screen.getByRole('button', { name:/Agent 编排与对比/ }))
    await userEvent.click(screen.getByRole('button', { name:/单 Agent.*对照基线/ }))
    expect(screen.getByText('单 Agent 使用相同模型、工具和验证门槛，可作为四角色编排的公平基线。')).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name:'最大轮次' })).toHaveValue(12)
    await userEvent.click(screen.getByRole('button', { name:'保存 Agent 编排' }))
    await waitFor(() => expect(screen.getByText('Agent 编排已保存，将从下一轮执行生效。')).toBeInTheDocument())
  })

  it('shows only the latest attempt progress and terminal result after retry', () => {
    const event = (type: string, summary: string, role?: 'planner' | 'reviewer' | 'single') => ({ type, session_id:'session-1', turn_id:'turn-1', summary, role, payload:{} })
    const visible = selectDisplayEvents([
      event('task_created', ''),
      event('agent_started', 'old plan', 'planner'),
      event('agent_started', 'old review', 'reviewer'),
      event('task_failed', 'old failure'),
      event('agent_started', 'new direct response', 'single'),
      event('task_finished', 'new success'),
    ])
    expect(visible.map(({ event: item }) => item.summary)).toEqual(['', 'new direct response', 'new success'])
  })

  it('hides a stale failure as soon as its retry starts', () => {
    const event = (type: string, summary: string, role?: 'planner' | 'coder') => ({ type, session_id:'session-1', turn_id:'turn-1', summary, role, payload:{} })
    const visible = selectDisplayEvents([
      event('task_created', ''),
      event('agent_started', 'old plan', 'planner'),
      event('task_failed', 'old failure'),
      event('agent_started', 'retry implementation', 'coder'),
    ])
    expect(visible.map(({ event: item }) => item.summary)).toEqual(['', 'retry implementation'])
  })
})
