import type { Page } from '@playwright/test'

/** 读取许可与文件响应分开模拟，文件请求不携带业务令牌 */
export async function mockDownloadPermits(page: Page) {
  await page.route('**/api/attachments/*/download-url*', async route => {
    const source = new URL(route.request().url())
    const id = source.pathname.split('/')[3]
    const target = new URL(`/objects/${id}`, source)
    target.search = source.search
    await route.fulfill({ json: { code: 0, message: 'success', data: { url: target.href, expires_in: 300 } } })
  })
}
