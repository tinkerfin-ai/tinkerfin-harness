import type { Page } from '@playwright/test'

declare global {
  interface Window {
    emitResourceChange: (topic: string, key: string) => number
  }
}

const installed = new WeakSet<Page>()

/** 为使用模拟业务接口的页面提供可取消的通知连接，不访问共享后端 */
export async function installNotificationStream(page: Page): Promise<void> {
  if (installed.has(page)) return
  installed.add(page)
  await page.addInitScript(() => {
    const original = window.fetch
    const readers = new Set<ReadableStreamDefaultController<Uint8Array>>()
    window.emitResourceChange = (topic, key) => {
      const data = { scope: { namespace: 'ns_1', owner_id: null }, topic, key, details: {} }
      for (const reader of readers) reader.enqueue(new TextEncoder().encode(`event: change\ndata: ${JSON.stringify(data)}\n\n`))
      return readers.size
    }
    window.fetch = async (input, init) => {
      const url = input instanceof Request ? input.url : String(input)
      if (new URL(url, location.href).pathname !== '/api/notifications') return original(input, init)
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
  })
}
