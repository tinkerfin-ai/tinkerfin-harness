import type { Page } from '@playwright/test'
import type { ResourceChange } from '../../../src/api/notifications'

declare global {
  interface Window {
    emitResourceChange: (topic: string, key: string, options?: Pick<ResourceChange, 'details' | 'scope'>) => number
  }
}

const installed = new WeakSet<Page>()

/** 提供可取消的通知连接和空运行基线；自动化用例可保留自己的运行接口 */
export async function installNotificationStream(page: Page, mockActiveRuns = true): Promise<void> {
  if (installed.has(page)) return
  installed.add(page)
  await page.addInitScript(({ mockActiveRuns }) => {
    const original = window.fetch
    const readers = new Set<ReadableStreamDefaultController<Uint8Array>>()
    window.emitResourceChange = (topic, key, options) => {
      const data = { scope: { namespace: 'ns_1', owner_id: null }, topic, key, details: {}, ...options }
      for (const reader of readers) reader.enqueue(new TextEncoder().encode(`event: change\ndata: ${JSON.stringify(data)}\n\n`))
      return readers.size
    }
    window.fetch = async (input, init) => {
      const url = input instanceof Request ? input.url : String(input)
      const path = new URL(url, location.href).pathname
      if (mockActiveRuns && path === '/api/automation/runs/active') {
        return new Response(JSON.stringify({ code: 0, message: 'success', data: { items: [], nextCursor: null } }), { headers: { 'Content-Type': 'application/json' } })
      }
      if (path !== '/api/notifications') return original(input, init)
      const request = new Request(input, init)
      let close: () => void
      return new Response(new ReadableStream<Uint8Array>({
        start(reader) {
          readers.add(reader)
          let closed = false
          const abort = () => { if (!closed) { closed = true; reader.close() }; close() }
          close = () => { closed = true; readers.delete(reader); request.signal.removeEventListener('abort', abort) }
          request.signal.addEventListener('abort', abort, { once: true })
          reader.enqueue(new TextEncoder().encode('event: ready\ndata: {}\n\n'))
        },
        cancel() { close() },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
  }, { mockActiveRuns })
}
