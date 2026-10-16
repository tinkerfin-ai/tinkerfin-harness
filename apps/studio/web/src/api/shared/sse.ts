/** 实时传输错误只携带稳定错误码，诊断信息不得直接作为界面提示 */
export class SseError extends Error {
  constructor(readonly code: 'stream_limit_exceeded' | 'stream_data_invalid', cause?: unknown) {
    super(code, { cause })
    this.name = 'SseError'
  }
}

export interface JsonSseFrame {
  id: string | null
  event: string | null
  data: unknown
}

const MAX_SSE_DATA_BYTES = 4 * 1024 * 1024
const MAX_SSE_LINE_BYTES = MAX_SSE_DATA_BYTES + 'data: '.length
const MAX_SSE_FRAME_BYTES = MAX_SSE_LINE_BYTES + 64
const MAX_SSE_FRAME_LINES = 4096

/**
 * 有界解析一个 JSON SSE 字节流，并在提前结束或取消时释放 reader
 *
 * 对话、轨迹和资源通知使用同一传输语法，事件内容由各自接口校验
 */
export async function* parseJsonSseStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<JsonSseFrame> {
  const decoder = new TextDecoder('utf-8', { fatal: false })
  const encoder = new TextEncoder()
  const reader = body.getReader()
  let currentLine = ''
  let currentLineBytes = 0
  let frameLines: string[] = []
  let frameBytes = 0
  let frameDataBytes = 0
  let previousWasCarriageReturn = false
  let aborted = false
  let naturalEof = false
  let cancelTask: Promise<void> | null = null

  const cancelReader = (reason: unknown) => {
    cancelTask ??= reader.cancel(reason).then(() => undefined).catch(() => undefined)
    return cancelTask
  }
  const onAbort = () => {
    aborted = true
    void cancelReader(signal?.reason)
  }
  if (signal?.aborted) onAbort()
  else signal?.addEventListener('abort', onAbort)

  const rejectLimit = (): never => {
    throw new SseError('stream_limit_exceeded')
  }

  const appendLineText = (text: string) => {
    if (!text) return
    currentLine += text
    currentLineBytes += encoder.encode(text).byteLength
    if (currentLineBytes > MAX_SSE_LINE_BYTES) rejectLimit()
  }

  const finishLine = () => {
    if (currentLine === '') {
      const parsed = frameLines.length > 0 ? parseFrame(frameLines) : null
      frameLines = []
      frameBytes = 0
      frameDataBytes = 0
      return parsed
    }
    if (frameLines.length >= MAX_SSE_FRAME_LINES) rejectLimit()
    frameBytes += currentLineBytes + 2
    if (frameBytes > MAX_SSE_FRAME_BYTES) rejectLimit()
    if (currentLine.startsWith('data:')) {
      const data = currentLine.slice(5).replace(/^ /, '')
      frameDataBytes += encoder.encode(data).byteLength + (frameDataBytes > 0 ? 1 : 0)
      if (frameDataBytes > MAX_SSE_DATA_BYTES) rejectLimit()
    }
    frameLines.push(currentLine)
    currentLine = ''
    currentLineBytes = 0
    return null
  }

  function* consumeText(text: string): Generator<JsonSseFrame> {
    let segmentStart = 0
    for (let index = 0; index < text.length; index += 1) {
      const character = text[index]
      if (previousWasCarriageReturn) {
        previousWasCarriageReturn = false
        if (character === '\n') {
          segmentStart = index + 1
          continue
        }
      }
      if (character === '\r') {
        appendLineText(text.slice(segmentStart, index))
        const parsed = finishLine()
        if (parsed) yield parsed
        previousWasCarriageReturn = true
        segmentStart = index + 1
      } else if (character === '\n') {
        appendLineText(text.slice(segmentStart, index))
        const parsed = finishLine()
        if (parsed) yield parsed
        segmentStart = index + 1
      }
    }
    appendLineText(text.slice(segmentStart))
  }

  try {
    while (true) {
      if (aborted) break
      const { done, value } = await reader.read()
      if (done) {
        naturalEof = !aborted
        break
      }
      for (const parsed of consumeText(decoder.decode(value, { stream: true }))) yield parsed
    }
    if (aborted) return
    for (const parsed of consumeText(decoder.decode())) yield parsed
    previousWasCarriageReturn = false
    if (currentLine !== '') finishLine()
    if (frameLines.length > 0) {
      const parsed = parseFrame(frameLines)
      if (parsed) yield parsed
    }
  } catch (error) {
    await cancelReader(error)
    throw error
  } finally {
    signal?.removeEventListener('abort', onAbort)
    if (!naturalEof) {
      await cancelReader(signal?.reason ?? new Error('事件流消费在 EOF 前结束'))
    } else if (cancelTask) {
      await cancelTask
    }
    reader.releaseLock()
  }
}

function parseFrame(lines: string[]): JsonSseFrame | null {
  let id: string | null = null
  let event: string | null = null
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('id:')) {
      id = line.slice(3).trim()
    } else if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
  }
  if (dataLines.length === 0) return null
  try {
    return {
      id,
      event,
      data: JSON.parse(dataLines.join('\n')) as unknown,
    }
  } catch (error) {
    throw new SseError('stream_data_invalid', error)
  }
}
