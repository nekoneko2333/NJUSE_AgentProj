import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FileWorkbench } from '../FileWorkbench'

describe('FileWorkbench', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })
  it('opens a text file in a tab and requires diff review before save', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/checkpoints')) return Promise.resolve(new Response('[]', { status:200 }))
      if (url.includes('/files/content')) return Promise.resolve(new Response(JSON.stringify({ path:'src/app.ts', content:'const a = 1\n', sha256:'a'.repeat(64), bytes:12 }), { status:200 }))
      throw new Error(`unexpected fetch ${url}`)
    }))
    const user = userEvent.setup()
    const view = render(<FileWorkbench sessionId="s1" entries={[{ path:'src', kind:'directory' }, { path:'src/app.ts', kind:'file' }]} onFilesChanged={async () => undefined}/>)
    await user.click(screen.getByRole('button', { name:'app.ts' }))
    const editor = await waitFor(() => {
      const candidate = view.container.querySelector('.file-editor')
      expect(candidate).toBeInstanceOf(HTMLTextAreaElement)
      return candidate as HTMLTextAreaElement
    })
    fireEvent.change(editor, { target:{ value:'const a = 2' } })
    await user.click(screen.getByRole('button', { name:/审阅保存/ }))
    expect(await screen.findByText(/保存前请审阅/)).toBeInTheDocument()
    expect(screen.getByText(/\+ const a = 2/)).toBeInTheDocument()
  })
})
