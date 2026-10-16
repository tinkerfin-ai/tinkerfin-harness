import type { Page } from '@playwright/test'
import type { ConversationHistoryDetail } from '../../../src/api/conversation/history'
import type { ConversationAgUiEvent } from '../../../src/api/conversation/types'

declare global {
  interface Window {
    studioTestRun: {
      ready: Promise<void>
      emit: (events: ConversationAgUiEvent[]) => void
      finish: () => void
    }
  }
}

/** 测试显式控制增量与终止，订阅取消时移除本次流的资源 */
export async function installLiveRun(page: Page, snapshot: ConversationHistoryDetail) {
  await page.addInitScript((serialized: string) => {
    const snapshot: ConversationHistoryDetail = JSON.parse(serialized)
    const originalFetch = window.fetch
    const subscribers = new Set<{
      enqueue: (chunk: string) => void
      close: () => void
    }>()
    let sequence = snapshot.asOfSeq
    let markReady: () => void
    const ready = new Promise<void>((resolve) => { markReady = resolve })
    window.studioTestRun = {
      ready,
      emit(events) {
        if (!subscribers.size) throw new Error('没有活动的测试流订阅')
        const chunk = events.map((event) => `id: ${++sequence}\ndata: ${JSON.stringify(event)}\n\n`).join('')
        subscribers.forEach((subscriber) => subscriber.enqueue(chunk))
      },
      finish() { [...subscribers].forEach((subscriber) => subscriber.close()) },
    }
    window.fetch = async (input, init) => {
      const request = new Request(input, init)
      const url = new URL(request.url)
      if (url.pathname !== `/api/conversation/${snapshot.threadId}/runs/${snapshot.headRunId}/events`) return originalFetch(input, init)
      const encoder = new TextEncoder()
      let cleanup: () => void
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          const subscriber = {
            enqueue: (chunk: string) => controller.enqueue(encoder.encode(chunk)),
            close: () => { cleanup(); controller.close() },
          }
          const abort = () => { cleanup(); controller.error(new DOMException('Aborted', 'AbortError')) }
          cleanup = () => {
            request.signal.removeEventListener('abort', abort)
            subscribers.delete(subscriber)
          }
          subscribers.add(subscriber)
          const value = { ...snapshot, taskTrace: url.searchParams.get('includeTaskTrace') === 'false' ? null : snapshot.taskTrace }
          controller.enqueue(encoder.encode(`event: snapshot\ndata: ${JSON.stringify({ type: 'snapshot', snapshot: value, replay: true })}\n\n`))
          controller.enqueue(encoder.encode(`id: ${++sequence}\ndata: ${JSON.stringify({ type: 'RUN_STARTED', threadId: snapshot.threadId, runId: snapshot.headRunId })}\n\n`))
          request.signal.addEventListener('abort', abort, { once: true })
          markReady()
        },
        cancel() { cleanup() },
      }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
  }, JSON.stringify(snapshot))
  return {
    emit: (...events: ConversationAgUiEvent[]) => page.evaluate(async (serialized: string) => {
      await window.studioTestRun.ready
      window.studioTestRun.emit(JSON.parse(serialized))
    }, JSON.stringify(events)),
    finish: () => page.evaluate(() => window.studioTestRun.finish()),
  }
}
