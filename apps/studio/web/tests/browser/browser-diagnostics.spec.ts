import { expect, test } from '@playwright/test'

import { fulfillExpectedHttpError, logBrowserDiagnostics } from './support/diagnostics'

test('预期 HTTP 异常只记录一次，同地址的未知失败和脚本异常仍报告错误', async ({ page }) => {
  const info: string[] = []
  const errors: string[] = []
  const originalInfo = console.info
  const originalError = console.error
  console.info = message => { info.push(String(message)) }
  console.error = message => { errors.push(String(message)) }
  try {
    logBrowserDiagnostics(page)
    logBrowserDiagnostics(page)
    await page.route('**/diagnostics', route => route.fulfill({
      contentType: 'text/html; charset=utf-8',
      body: '<button type="button" onclick="throw new Error(\'脚本执行失败\')">触发脚本异常</button>',
    }))
    let requests = 0
    await page.route('**/diagnostic-api', async route => {
      requests += 1
      if (requests === 1) await fulfillExpectedHttpError(route, 503, '验证错误反馈')
      else await route.fulfill({ status: 503, body: '{}' })
    })
    await page.goto('/diagnostics')
    await page.evaluate(async () => {
      await fetch('/diagnostic-api')
      await fetch('/diagnostic-api')
      console.error('Failed to load resource: the server responded with a status of 503 (Service Unavailable)')
    })
    await page.getByRole('button', { name: '触发脚本异常' }).click()
    const url = new URL('/diagnostic-api', page.url()).href
    await expect.poll(() => info).toEqual([
      `browser 预期异常: 503 GET ${url}（验证错误反馈）`,
    ])
    await expect.poll(() => errors).toEqual([
      `browser response: 503 GET ${url}`,
      'browser console: Failed to load resource: the server responded with a status of 503 (Service Unavailable)',
      'browser pageerror: 脚本执行失败',
    ])
  } finally {
    console.info = originalInfo
    console.error = originalError
  }
})
