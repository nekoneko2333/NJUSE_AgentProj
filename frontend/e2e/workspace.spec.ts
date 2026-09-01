import { expect, test } from '@playwright/test'

const session = {
  id:'session-1', title:'演示任务', task:'检查界面', workspace:'C:\\demo', locale:'zh-CN', status:'finished', memory_summary:'', created_at:'now', updated_at:'now', command_mode:'auto',
  turns:[{ id:'turn-1', session_id:'session-1', position:1, user_content:'检查界面', assistant_summary:'完成', status:'finished', created_at:'now' }],
  events:[{ type:'task_created', session_id:'session-1', turn_id:'turn-1', summary:'', payload:{ task:'检查界面', position:1 } }, { type:'task_finished', session_id:'session-1', turn_id:'turn-1', summary:'## 验证完成\n\nMarkdown 已渲染。', payload:{ changed_files:[] } }],
}

test('login, restore a conversation, and keep all responsive layouts in bounds', async ({ page }) => {
  let authenticated = false
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/auth/status')) return route.fulfill({ json:{ authenticated, username:authenticated ? 'moss' : '' } })
    if (url.pathname.endsWith('/auth/login')) { authenticated = true; return route.fulfill({ json:{ username:'moss' } }) }
    if (url.pathname.endsWith('/sessions')) return route.fulfill({ json:[{ ...session, turn_count:1 }] })
    if (url.pathname.endsWith('/sessions/session-1')) return route.fulfill({ json:session })
    if (url.pathname.includes('/workspace-files')) return route.fulfill({ json:{ items:[{ path:'src',kind:'directory' },{ path:'src/app.ts',kind:'file' }] } })
    if (url.pathname.endsWith('/checkpoints')) return route.fulfill({ json:[] })
    return route.fulfill({ status:404, json:{ detail:'not_found' } })
  })
  await page.goto('/')
  await page.getByLabel('用户名').fill('moss')
  await page.getByLabel('密码').fill('mosscode')
  await page.getByRole('button', { name:'登录 MossCode' }).click()
  await page.getByRole('button', { name:/演示任务/ }).click()
  await expect(page.getByText('验证完成')).toBeVisible()
  await expect(page.locator('.markdown h2')).toHaveText('验证完成')
  for (const viewport of [{ width:1440,height:900 },{ width:900,height:800 },{ width:390,height:844 }]) {
    await page.setViewportSize(viewport)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await expect(page.locator('.conversation')).toBeVisible()
    await expect(page.locator('.inspector')).toBeVisible()
  }
})
