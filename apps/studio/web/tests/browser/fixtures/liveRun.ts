import type { Page } from '@playwright/test'
import type { ConversationHistoryDetail } from '../../../src/api/conversation/history'

/** 布局样本维持可取消的运行订阅，由测试决定何时离开页面 */
export async function installLiveRun(page: Page, snapshot: ConversationHistoryDetail) {
  await page.addInitScript((serialized: string) => {
    const snapshot: ConversationHistoryDetail = JSON.parse(serialized)
    const originalFetch = window.fetch
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      const url = new URL(request.url)
      if (url.pathname !== `/api/conversation/${snapshot.threadId}/runs/${snapshot.headRunId}/events`) return originalFetch(input, init)
      const encoder = new TextEncoder()
      const onAbort = (controller: ReadableStreamDefaultController<Uint8Array>) => () => controller.error(new DOMException('Aborted', 'AbortError'))
      let abort: (() => void) | undefined
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          const value = { ...snapshot, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : snapshot.taskTrace }
          controller.enqueue(encoder.encode(`event: snapshot\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: value, replay: true })}\n\n`))
          controller.enqueue(encoder.encode(`id: 1\ndata: ${JSON.stringify({ type: 'RUN_STARTED', threadId: snapshot.threadId, runId: snapshot.headRunId })}\n\n`))
          abort = onAbort(controller)
          request.signal.addEventListener('abort', abort, { once: true })
        },
        cancel() { if (abort) request.signal.removeEventListener('abort', abort) },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
  }, JSON.stringify(snapshot))
}
