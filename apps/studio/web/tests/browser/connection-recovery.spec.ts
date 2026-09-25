import { expect, test } from '@playwright/test'

const user = { user_id: 1, username: 'connection-test', display_name: '连接验收', avatar_url: null, roles: [], disabled: false }

for (const theme of ['light', 'dark']) {
  for (const width of [320, 768, 1024, 1440]) {
    test(`连接失联提示与手动恢复 ${theme} ${width}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await page.addInitScript(({ user, theme }) => {
        localStorage.setItem('tinkerfin:theme', theme)
        localStorage.setItem('tinkerfin.auth.session', JSON.stringify({
          token: 'browser-token', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user,
        }))
      }, { user, theme })
      const submitted: unknown[] = []
      let cancelled = 0
      await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname
        if (path === '/api/conversation/chat') {
          submitted.push(route.request().postDataJSON())
          await route.abort('connectionreset')
          return
        }
        if (path.endsWith('/cancel')) cancelled += 1
        let data: unknown = {}
        if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00.000Z', user }
        else if (path === '/api/models') data = { items: [{ modelId: 'main', displayName: 'Main',connectionId: 'test-provider', connectionDisplayName: '测试提供方', reasoningEnabled: true, isDefault: true }], defaultModelId: 'main' }
        else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
        else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
        await route.fulfill({ json: { code: 0, message: 'success', data } })
      })
      await page.goto('/')
      const input = page.getByRole('textbox', { name: '消息输入' })
      await expect(input).toBeEnabled()
      await input.fill('检查连接恢复')
      await page.getByRole('button', { name: '发送消息' }).click()
      const recovery = page.getByRole('button', { name: '恢复连接', exact: true })
      await expect(recovery).toBeVisible()
      await expect(page.getByRole('status')).toContainText('尚无法确认任务状态')
      await expect(page.getByLabel('任务仍在继续')).toHaveCount(0)
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
      expect(submitted).toHaveLength(1)
      const box = (await recovery.boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(width)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
      await recovery.focus()
      await expect(recovery).toBeFocused()
      await page.screenshot({ path: testInfo.outputPath(`connection-${theme}-${width}.png`) })
      if (width === 320) {
        const device = await page.context().newCDPSession(page)
        await device.send('Emulation.setTouchEmulationEnabled', { enabled: true })
        await expect.poll(async () => (await recovery.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44)
        expect((await recovery.boundingBox())!.width).toBeGreaterThanOrEqual(44)
        await device.detach()
      }
      await recovery.press('Enter')
      await expect.poll(() => submitted.length).toBeGreaterThan(1)
      expect(submitted[1]).toEqual(submitted[0])
      expect(cancelled).toBe(0)
    })
  }
}
