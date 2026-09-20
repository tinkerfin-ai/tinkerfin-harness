import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { cancelConversationRun, startConversationRun } from './client'
import type { ChatRequestPayload } from './types'
import { clearAuthSession, saveAuthSession } from '../../auth/session'

const requestPayload: ChatRequestPayload = {
  threadId: 'thread-conflict',
  runId: 'run-conflict',
  state: {},
  messages: [{ id: 'request-run-conflict', role: 'user', content: '继续执行' }],
  tools: [],
  context: [],
  forwardedProps: { accessMode: 'write_approval', model: 'main', command: { plan: 'off' } },
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
      tokenType: 'Bearer',
      expiresAt: '2099-01-01T00:00:00.000Z',
      user: { user_id: 7, username: 'yunsan', display_name: '云杉', avatar_url: null, roles: [], disabled: false },
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

  it('sends the durable Last-Event-ID only for a reconnect attempt', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get('Last-Event-ID')).toBe('41')
      return new Response('', { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
    })
    vi.stubGlobal('fetch', fetchMock)

    for await (const item of startConversationRun(requestPayload, undefined, 41)) void item

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

  it('rejects a JSON business error instead of treating it as an empty SSE stream', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({
        code: 1_001_004_002,
        message: '会话当前状态不允许启动新的运行',
        data: null,
      }),
      {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      },
    )))

    await expect(consumeStream()).rejects.toMatchObject({
      code: 1_001_004_002,
      message: '会话当前状态不允许启动新的运行',
    })
  })

  it.each([
    ['LF', '\n'],
    ['CRLF', '\r\n'],
    ['CR', '\r'],
  ])('parses %s-delimited events and preserves their SSE sequence', async (_name, eol) => {
    const first = JSON.stringify({
      type: 'RUN_STARTED',
      threadId: 'thread-conflict',
      runId: 'run-conflict',
    })
    const second = JSON.stringify({
      type: 'RUN_FINISHED',
      threadId: 'thread-conflict',
      runId: 'run-conflict',
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(
      `id: 41${eol}data: ${first}${eol}${eol}id: 42${eol}data: ${second}${eol}${eol}`,
    )))

    const received = []
    for await (const item of startConversationRun(requestPayload)) received.push(item)

    expect(received).toEqual([
      { seq: 41, event: JSON.parse(first) },
      { seq: 42, event: JSON.parse(second) },
    ])
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

  it('joins multiline CRLF data without leaking carriage returns into JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([
      'id: 9',
      'data: {"type":"RUN_STARTED",',
      'data: "threadId":"thread-conflict","runId":"run-conflict"}',
      '',
      '',
    ].join('\r\n'))))

    const received = []
    for await (const item of startConversationRun(requestPayload)) received.push(item)

    expect(received).toEqual([{
      seq: 9,
      event: {
        type: 'RUN_STARTED',
        threadId: 'thread-conflict',
        runId: 'run-conflict',
      },
    }])
  })

  it('reports malformed AG-UI data with a stable user-facing error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse('data: ping\n\n')))

    await expect(consumeStream()).rejects.toMatchObject({
      code: 'stream_data_invalid',
    })
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

  it('does not cancel the underlying stream after natural EOF', async () => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(
          'data: {"type":"RUN_STARTED","threadId":"thread-conflict","runId":"run-conflict"}\n\n',
        ))
        controller.close()
      },
      cancel,
    })
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(body)))

    await consumeStream()

    expect(cancel).not.toHaveBeenCalled()
  })

  it('accepts one valid event whose JSON data is exactly 4 MiB', async () => {
    const limit = 4 * 1024 * 1024
    const empty = JSON.stringify({
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'message-large',
      delta: '',
    })
    const event = JSON.stringify({
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'message-large',
      delta: 'x'.repeat(limit - new TextEncoder().encode(empty).byteLength),
    })
    expect(new TextEncoder().encode(event).byteLength).toBe(limit)
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(`data: ${event}\n\n`)))

    const received = []
    for await (const item of startConversationRun(requestPayload)) received.push(item)

    expect(received).toHaveLength(1)
    expect(received[0]?.event).toMatchObject({
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'message-large',
    })
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

  it.each([
    ['未知事件', { type: 'STEP_STARTED', stepName: 'model' }],
    ['缺少必填字段', { type: 'RUN_STARTED', runId: 'run-conflict' }],
    ['字段类型错误', { type: 'TEXT_MESSAGE_CONTENT', messageId: 'message-1', delta: 1 }],
  ])('rejects structurally invalid %s JSON before it reaches the reducer', async (_name, event) => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(
      `data: ${JSON.stringify(event)}\n\n`,
    )))

    await expect(consumeStream()).rejects.toMatchObject({
      code: 'stream_event_invalid',
    })
  })

  it('streams the complete compiled-subgraph Tool lifecycle', async () => {
    const source = {
      kind: 'compiled_subgraph',
      nodeName: 'create_plan',
      graphNamespace: ['create_plan:graph-task-1'],
      graphTaskId: 'graph-task-1',
      parentGraphNamespace: [],
    }
    const events = [
      {
        type: 'TOOL_CALL_START',
        toolCallId: 'planner-outcome-1',
        toolCallName: 'submit_plan',
        parentMessageId: 'planner-message-1',
      },
      {
        type: 'TOOL_CALL_ARGS',
        toolCallId: 'planner-outcome-1',
        delta: '{',
      },
      {
        type: 'TOOL_CALL_END',
        toolCallId: 'planner-outcome-1',
      },
      {
        type: 'TOOL_CALL_RESULT',
        toolCallId: 'planner-outcome-1',
        messageId: 'planner-result-1',
        content: 'Returning structured response',
        role: 'tool',
      },
    ].map((event) => ({
      ...event,
      rawEvent: {
        streamMode: 'messages',
        runId: 'run-conflict',
        langgraphNode: 'model',
        source,
      },
    }))
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse(
      events.map((event, index) => (
        `id: ${index + 11}\ndata: ${JSON.stringify(event)}\n\n`
      )).join(''),
    )))

    const received = []
    for await (const item of startConversationRun(requestPayload)) received.push(item)

    expect(received.map(({ seq, event }) => [seq, event.type])).toEqual([
      [11, 'TOOL_CALL_START'],
      [12, 'TOOL_CALL_ARGS'],
      [13, 'TOOL_CALL_END'],
      [14, 'TOOL_CALL_RESULT'],
    ])
  })

  it.each([
    ['/backend', '/backend/api/conversation/chat'],
    ['backend', '/backend/api/conversation/chat'],
    ['https://api.example.test/backend', 'https://api.example.test/backend/api/conversation/chat'],
  ])('applies the configured API base exactly once: %s', async (apiBase, expectedUrl) => {
    vi.stubEnv('VITE_API_BASE_URL', apiBase)
    vi.resetModules()
    const { startConversationRun: startConfiguredRun } = await import('./client')
    let requestedUrl: RequestInfo | URL | undefined
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      requestedUrl = input
      return new Response(
        `data: ${JSON.stringify({
          type: 'RUN_STARTED',
          threadId: 'thread-conflict',
          runId: 'run-conflict',
        })}\n\n`,
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    for await (const streamedEvent of startConfiguredRun(requestPayload)) {
      expect(streamedEvent.event.type).toBe('RUN_STARTED')
    }

    expect(requestedUrl).toBe(expectedUrl)
  })
})
