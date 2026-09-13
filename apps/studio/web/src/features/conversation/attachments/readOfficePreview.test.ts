import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readOfficePreview } from './readOfficePreview'
import { DOCUMENT_PREVIEW_TIMEOUT, type OfficePreviewResponse } from './documentPreview'

class ControlledWorker {
  static instances: ControlledWorker[] = []
  onmessage: ((event: MessageEvent<OfficePreviewResponse>) => void) | null = null
  onerror: (() => void) | null = null
  onmessageerror: (() => void) | null = null
  postMessage = vi.fn()
  terminate = vi.fn()
  constructor() { ControlledWorker.instances.push(this) }
}

beforeEach(() => {
  vi.useFakeTimers()
  ControlledWorker.instances = []
  vi.stubGlobal('Worker', ControlledWorker)
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('预览解析资源所有权', () => {
  it('成功后立即释放线程，传递完整文件并转移缓冲区', async () => {
    const controller = new AbortController()
    const bytes = new ArrayBuffer(3)
    const result = readOfficePreview(bytes, 'docx', controller.signal)
    const worker = ControlledWorker.instances[0]!
    expect(worker.postMessage).toHaveBeenCalledWith({ bytes, format: 'docx' }, [bytes])
    worker.onmessage?.(new MessageEvent('message', { data: { content: { kind: 'docx', html: '<p>营收</p>' } } }))
    await expect(result).resolves.toEqual({ kind: 'docx', html: '<p>营收</p>' })
    controller.abort()
    expect(worker.terminate).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })
  it.each(['abort', 'timeout', 'parse', 'worker'] as const)('%s 终止解析并释放线程与超时任务', async (mode) => {
    const controller = new AbortController()
    const result = readOfficePreview(new ArrayBuffer(0), 'xlsx', controller.signal)
    const rejection = expect(result).rejects.toMatchObject(mode === 'abort' ? { name: 'AbortError' } : { code: mode === 'timeout' ? 'timeout' : 'invalid' })
    const worker = ControlledWorker.instances[0]!
    if (mode === 'abort') controller.abort()
    else if (mode === 'timeout') await vi.advanceTimersByTimeAsync(DOCUMENT_PREVIEW_TIMEOUT)
    else if (mode === 'parse') worker.onmessage?.(new MessageEvent('message', { data: { error: 'invalid' } }))
    else worker.onerror?.()
    await rejection
    expect(worker.terminate).toHaveBeenCalledOnce()
    expect(worker.onmessage).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('已关闭的预览不再创建解析线程', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(readOfficePreview(new ArrayBuffer(0), 'docx', controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(ControlledWorker.instances).toHaveLength(0)
  })
})
