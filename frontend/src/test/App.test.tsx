import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))

describe('App conversation stability', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mosscode.lastSession', 'session-1')
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/auth/status')) return json({ authenticated: true, username: 'moss' })
      if (url.endsWith('/sessions')) return json([{ id:'session-1', title:'测试', task:'原任务', workspace:'C:\\workspace', locale:'zh-CN', status:'finished', updated_at:'now', turn_count:1, command_mode:'auto' }])
      if (url.endsWith('/sessions/session-1')) return json({ id:'session-1', title:'测试', task:'追加任务', workspace:'C:\\workspace', locale:'zh-CN', status:'finished', memory_summary:'', created_at:'now', updated_at:'now', command_mode:'auto', turns:[], events:[{ type:'task_created', session_id:'session-1', turn_id:'turn-1', summary:'', payload:{ task:'原任务', position:1 } }, { type:'task_finished', session_id:'session-1', turn_id:'turn-1', summary:'最终答复', payload:{} }, { type:'task_created', session_id:'session-1', turn_id:'turn-2', summary:'', payload:{ task:'追加任务', position:2 } }, { type:'task_finished', session_id:'session-1', turn_id:'turn-2', summary:'第二轮答复', payload:{} }] })
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
})
