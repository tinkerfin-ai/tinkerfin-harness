import { readFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

import { installNotificationStream } from './fixtures/notifications'
import { installProjectScope } from './fixtures/projects'

for (const hasTouch of [false, true]) {
  test.describe(hasTouch ? '触控' : '鼠标', () => {
    test.use({ hasTouch })
    for (const theme of ['light', 'dark']) {
      test(`用户头像的选图、取消、保存和默认图标 ${theme}`, async ({ page }, info) => {
        const user = { user_id: 7, username: 'yunsan', avatar_url: null as string | null, roles: [], disabled: false }
        const image = await readFile('public/brand/tinkerfin-mark.png')
        let failSave = true
        let failImage = false
        let savedImage = 0
        await page.emulateMedia({ reducedMotion: 'reduce' })
        await page.addInitScript(({ user, theme }) => {
          localStorage.setItem('tinkerfin.auth.session', JSON.stringify({ token: 'profile-test', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00Z', user }))
          localStorage.setItem('tinkerfin:language', 'zh-CN')
          localStorage.setItem('tinkerfin:theme', theme)
        }, { user, theme })
        await page.route('**/api/**', async route => {
          const path = new URL(route.request().url()).pathname
          let data: unknown = {}
          if (path === '/api/auth/me') data = { expires_at: '2099-01-01T00:00:00Z', user }
          else if (path === '/api/models') data = { items: [], defaultModelId: null }
          else if (path === '/api/conversation/config') data = { dayRanges: [7, 30] }
          else if (path === '/api/conversation/history') data = { items: [], nextCursor: null }
          else if (path === '/api/skills/installations') data = []
          else if (path === '/api/user/me/avatar') {
            expect(route.request().postDataBuffer()).toEqual(image)
            if (failSave) {
              failSave = false
              await route.fulfill({ status: 503, json: { code: 503, message: '保存失败，请重试', data: null } })
              return
            }
            user.avatar_url = `https://files.example.test/tinkerfin/avatars/${String(++savedImage).padStart(32, '0')}.jpg`
            data = user
          }
          await route.fulfill({ json: { code: 0, message: 'success', data } })
        })
        await page.route('https://files.example.test/tinkerfin/avatars/**', route => route.fulfill(failImage ? { status: 404 } : { contentType: 'image/png', body: image }))
        await installNotificationStream(page)
        await installProjectScope(page)
        await page.goto('/')
        await page.getByRole('button', { name: '打开用户菜单' }).click()
        await page.getByRole('menuitem', { name: '设置', exact: true }).click()
        const dialog = page.getByRole('dialog', { name: '设置', exact: true })
        await expect(dialog.getByText('yunsan', { exact: true })).toBeVisible()
        await expect(dialog.getByRole('textbox')).toHaveCount(0)
        await expect(dialog.getByRole('button', { name: '保存', exact: true })).toHaveCount(0)
        for (const width of [320, 768, 1024, 1440]) {
          await page.setViewportSize({ width, height: 900 })
          await expect(dialog.getByRole('button', { name: '更换头像' })).toBeVisible()
          expect((await dialog.getByRole('button', { name: '更换头像' }).boundingBox())!.height).toBe(hasTouch ? 48 : 36)

          const bounds = await dialog.getByText('yunsan', { exact: true }).boundingBox()
          expect(bounds!.width).toBeGreaterThan(70)
          expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
          if (hasTouch) {
            const smallTargets = await dialog.getByRole('button').evaluateAll(buttons => buttons.filter(button => {
              const box = button.getBoundingClientRect()
              return box.width > 0 && box.height > 0 && (box.width < 44 || box.height < 44)
            }).map(button => button.textContent))
            expect(smallTargets).toEqual([])
          }
          await page.screenshot({ path: info.outputPath(`profile-${hasTouch ? 'touch' : 'mouse'}-${theme}-${width}.png`), animations: 'disabled' })
        }
        await dialog.locator('input[type=file]').setInputFiles({ name: 'avatar.png', mimeType: 'image/png', buffer: image })
        await expect(dialog.getByRole('button', { name: '保存', exact: true })).toBeEnabled()
        await page.evaluate(() => {
          const stored = JSON.parse(localStorage.getItem('tinkerfin.auth.session')!)
          stored.user.avatar_url = 'https://files.example.test/tinkerfin/avatars/ffffffffffffffffffffffffffffffff.jpg'
          localStorage.setItem('tinkerfin.auth.session', JSON.stringify(stored))
          window.dispatchEvent(new StorageEvent('storage', { key: 'tinkerfin.auth.session' }))
        })
        await expect(dialog.getByRole('button', { name: '保存', exact: true })).toBeEnabled()
        await dialog.press('Escape')
        await expect(dialog.getByRole('group', { name: '头像尚未保存' })).toBeVisible()
        await expect(dialog.getByRole('button', { name: '继续编辑' })).toBeFocused()
        await dialog.getByRole('button', { name: '继续编辑' }).click()
        await dialog.getByRole('button', { name: '保存', exact: true }).click()
        await expect(dialog.getByRole('alert')).toHaveText('保存失败，请重试')
        await dialog.getByRole('button', { name: '保存', exact: true }).click()
        await expect(dialog.getByRole('status')).toHaveText('头像已保存')
        await expect(dialog.getByRole('button', { name: '更换头像' })).toBeFocused()
        const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('tinkerfin.auth.session')!))
        expect(stored.user.avatar_url).toBe(user.avatar_url)
        await expect(dialog.locator('img')).toHaveAttribute('src', user.avatar_url!)
        await expect(dialog.getByRole('button', { name: '移除头像' })).toHaveCount(0)
        await dialog.locator('input[type=file]').setInputFiles({ name: 'avatar.png', mimeType: 'image/png', buffer: image })
        await dialog.getByRole('button', { name: '取消', exact: true }).click()
        await expect(dialog.locator('img')).toBeVisible()
        failImage = true
        await dialog.locator('input[type=file]').setInputFiles({ name: 'avatar.png', mimeType: 'image/png', buffer: image })
        await dialog.getByRole('button', { name: '保存', exact: true }).click()
        await expect(dialog.getByRole('status')).toHaveText('头像已保存')
        await expect(dialog.getByRole('button', { name: '移除头像' })).toHaveCount(0)
        await expect(dialog.locator('img')).toHaveCount(0)
      })
    }

  })
}
