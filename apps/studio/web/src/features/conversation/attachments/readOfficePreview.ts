import { DOCUMENT_PREVIEW_TIMEOUT, DocumentPreviewError, type OfficeFormat, type OfficePreview, type OfficePreviewResponse } from './documentPreview'

/** 解析工作在独立线程中进行；超时或关闭预览时立即终止 */
export function readOfficePreview(bytes: ArrayBuffer, format: OfficeFormat, signal: AbortSignal): Promise<OfficePreview> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return }
    const worker = new Worker(new URL('./documentPreview.worker.ts', import.meta.url), { type: 'module' })
    let settled = false
    const finish = (content?: OfficePreview, error?: Error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      worker.onmessage = null
      worker.onerror = null
      worker.onmessageerror = null
      worker.terminate()
      if (error) reject(error)
      else if (content) resolve(content)
    }
    const onAbort = () => finish(undefined, new DOMException('Aborted', 'AbortError'))
    const timer = setTimeout(() => finish(undefined, new DocumentPreviewError('timeout')), DOCUMENT_PREVIEW_TIMEOUT)
    signal.addEventListener('abort', onAbort, { once: true })
    worker.onmessage = (event: MessageEvent<OfficePreviewResponse>) => {
      if ('error' in event.data) finish(undefined, new DocumentPreviewError(event.data.error))
      else finish(event.data.content)
    }
    worker.onerror = () => finish(undefined, new DocumentPreviewError('invalid'))
    worker.onmessageerror = () => finish(undefined, new DocumentPreviewError('invalid'))
    try { worker.postMessage({ bytes, format }, [bytes]) }
    catch { finish(undefined, new DocumentPreviewError('invalid')) }
  })
}
