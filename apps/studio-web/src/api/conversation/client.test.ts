import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { cancelConversationRun, startConversationRun } from './client'
import type { ChatRequestPayload } from './types'

const requestPayload: ChatRequestPayload = {
  threadId: 'thread-conflict',
  runId: 'run-conflict',
  state: {},
  messages: [{ id: 'request-run-conflict', role: 'user', content: '继续执行' }],
  tools: [],
  context: [],
  forwardedProps: {projectId: 'project-1',  skillIds: [], accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
}

async function consumeStream(): Promise<void> {
  for await (const event of startConversationRun(requestPayload)) {
    // 消费完整流，确保非 SSE 响应不会被当成空流吞掉
    void event
  }
}

function sseResponse(body: BodyInit) {
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

describe('conversation stream client', () => {
  beforeEach(() => {
    saveAuthSession({
      token: 'conversation-token',
      serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: { user_id: 7, username: 'yunsan', avatar_url: null, roles: [], disabled: false },
    })
  })

  afterEach(() => {
    clearAuthSession()
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('sends the session bearer token with the conversation stream', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer conversation-token')
      return new Response('', { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
    })
    vi.stubGlobal('fetch', fetchMock)

    await consumeStream()

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('requests durable cancellation for the exact thread and run', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init)
      expect(new URL(request.url).pathname).toBe('/api/conversation/thread%2Fone/runs/run%2Fone/cancel')
      expect(request.method).toBe('POST')
      return new Response(JSON.stringify({
        code: 0,
        message: 'success',
        data: { cancelled: true },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelConversationRun('thread/one', 'run/one')).resolves.toEqual({ cancelled: true })
  })

  it('parses a CRLF event incrementally when the delimiter crosses chunks', async () => {
    const encoder = new TextEncoder()
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller
      },
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(body)))
    const iterator = startConversationRun(requestPayload)[Symbol.asyncIterator]()
    const pending = iterator.next()

    streamController?.enqueue(encoder.encode(
      'id: 7\r\ndata: {"type":"RUN_STARTED","threadId":"thread-conflict",',
    ))
    streamController?.enqueue(encoder.encode('"runId":"run-conflict"}\r'))
    streamController?.enqueue(encoder.encode('\n\r\n'))
    try {
      const result = await pending
      expect(result.value).toMatchObject({ seq: 7, event: { type: 'RUN_STARTED' } })
    } finally {
      streamController?.close()
      await iterator.return?.(undefined)
    }
  })

  it('cancels the underlying stream after malformed data aborts consumption', async () => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: ping\n\n'))
      },
      cancel,
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(body)))

    await expect(consumeStream()).rejects.toMatchObject({
      code: 'stream_data_invalid',
    })
    expect(cancel).toHaveBeenCalledOnce()
    expect(cancel.mock.calls[0]?.[0]).toBeInstanceOf(Error)
  })

  it('cancels the underlying stream when the consumer returns before EOF', async () => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(
          'data: {"type":"RUN_STARTED","threadId":"thread-conflict","runId":"run-conflict"}\n\n',
        ))
      },
      cancel,
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(body)))
    const iterator = startConversationRun(requestPayload)[Symbol.asyncIterator]()

    await expect(iterator.next()).resolves.toMatchObject({
      done: false,
      value: { event: { type: 'RUN_STARTED' } },
    })
    await iterator.return?.(undefined)

    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([
    ['single line', `data: ${'x'.repeat((4 * 1024 * 1024) + 1)}`],
    ['frame line count', `${'x:\n'.repeat(4097)}\n`],
    ['frame data bytes', `${Array.from(
      { length: 5 },
      () => `data: ${'x'.repeat(900 * 1024)}`,
    ).join('\n')}\n\n`],
  ])('bounds %s and cancels the stream', async (_name, content) => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(content))
      },
      cancel,
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(body)))

    await expect(consumeStream()).rejects.toMatchObject({
      code: 'stream_limit_exceeded',
    })
    expect(cancel).toHaveBeenCalledOnce()
  })
})
