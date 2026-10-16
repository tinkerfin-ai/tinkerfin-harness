import type { Page, Request, Route } from '@playwright/test'

const expectedHttpErrors = new WeakMap<Request, { status: number; reason: string }>()
const loggedPages = new WeakSet<Page>()

/** 只标记实际注入的失败请求，其他请求即使地址和状态码相同也仍报告错误 */
export async function fulfillExpectedHttpError(route: Route, status: number, reason: string) {
  expectedHttpErrors.set(route.request(), { status, reason })
  await route.fulfill({ status, contentType: 'application/json', body: '{}' })
}

/** 每个测试页面安装一次；HTTP 失败由响应日志完整记录，脚本错误独立报告 */
export function logBrowserDiagnostics(page: Page) {
  if (loggedPages.has(page)) return
  loggedPages.add(page)
  page.on('console', message => {
    if (message.type() !== 'error') return
    // Chromium 的 HTTP 资源错误没有脚本参数；省略与响应重复的一行，保留脚本主动打印的同名错误
    if (message.args().length === 0 && /^Failed to load resource: the server responded with a status of \d{3} \([^\n]*\)$/.test(message.text())) return
    console.error(`browser console: ${message.text()}`)
  })
  page.on('pageerror', error => console.error(`browser pageerror: ${error.message}`))
  page.on('response', response => {
    if (response.status() < 400) return
    const request = response.request()
    const expected = expectedHttpErrors.get(request)
    expectedHttpErrors.delete(request)
    if (expected?.status === response.status()) {
      console.info(`browser 预期异常: ${response.status()} ${request.method()} ${response.url()}（${expected.reason}）`)
    } else {
      console.error(`browser response: ${response.status()} ${request.method()} ${response.url()}`)
    }
  })
}
