import { act, render, renderHook, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { useAttachments, type DraftAttachment } from './useAttachments'
import { removeDraftAttachment, uploadAttachment } from './attachments/client'

vi.mock('./attachments/client', () => ({
  uploadAttachment: vi.fn(),
  removeDraftAttachment: vi.fn().mockResolvedValue(undefined),
}))

describe('attachment uploads', () => {
  it('uploads skill ZIP files through the existing attachment channel', async () => {
    vi.mocked(uploadAttachment).mockResolvedValueOnce({ id: 'zip', name: 'reports.zip', mime_type: 'application/zip', size_bytes: 3 })
    const { result } = renderHook(() => useAttachments('project-1'))
    act(() => result.current.addFiles([new File(['zip'], 'reports.zip', { type: 'application/zip' })]))
    await waitFor(() => expect(result.current.attachments[0]?.state).toBe('ready'))
    expect(result.current.attachments[0].attachment?.mime_type).toBe('application/zip')
  })
  it('retains a failed file and retries it into a ready attachment', async () => {
    vi.mocked(uploadAttachment)
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({
        id: 'stored',
        name: 'chart.png',
        mime_type: 'image/png',
        size_bytes: 3,
      })
    const onError = vi.fn()
    const { result } = renderHook(() => useAttachments('project-1', onError))
    act(() =>
      result.current.addFiles([
        new File(['png'], 'chart.png', { type: 'image/png' }),
      ]),
    )
    await waitFor(() =>
      expect(result.current.attachments[0]?.state).toBe('error'),
    )
    expect(onError).toHaveBeenCalledExactlyOnceWith('network')
    act(() => result.current.retryAttachment(result.current.attachments[0].id))
    await waitFor(() =>
      expect(result.current.attachments[0]?.state).toBe('ready'),
    )
    expect(result.current.attachments[0].attachment?.id).toBe('stored')
  })
  it('rejects unsupported files and oversized images without uploading', () => {
    vi.mocked(uploadAttachment).mockClear()
    const onError = vi.fn()
    const { result } = renderHook(() => useAttachments('project-1', onError))
    act(() =>
      result.current.addFiles([
        new File(['x'], 'macro.exe'),
        new File([new Uint8Array(10 * 1024 ** 2 + 1)], 'big.png'),
      ]),
    )
    expect(result.current.attachments).toEqual([])
    expect(onError).toHaveBeenCalledExactlyOnceWith('最多 5 个附件，单个 10 MiB，合计 25 MiB')
    expect(uploadAttachment).not.toHaveBeenCalled()
  })
  it('limits a draft to five attachments', () => {
    vi.mocked(uploadAttachment).mockImplementation(() => new Promise(() => {}))
    const onError = vi.fn()
    const { result } = renderHook(() => useAttachments('project-1', onError))
    const files = Array.from({ length: 6 }, (_, index) => new File(['x'], `file-${index}.pdf`, { type: 'application/pdf' }))

    act(() => result.current.addFiles(files))

    expect(result.current.attachments).toHaveLength(5)
    expect(onError).toHaveBeenCalledExactlyOnceWith('最多 5 个附件，单个 10 MiB，合计 25 MiB')
  })
  it('does not put an old deletion error into a new conversation draft', async () => {
    let rejectDelete: (error: Error) => void = () => undefined
    vi.mocked(uploadAttachment).mockResolvedValueOnce({id: 'stored', name: 'a.png', mime_type: 'image/png', size_bytes: 3})
    vi.mocked(removeDraftAttachment).mockImplementationOnce(() => new Promise((_, reject) => { rejectDelete = reject }))
    const onError = vi.fn()
    const { result } = renderHook(() => useAttachments('project-1', onError))
    act(() => result.current.addFiles([new File(['png'], 'a.png')]))
    await waitFor(() => expect(result.current.attachments[0]?.state).toBe('ready'))
    act(() => result.current.removeAttachment(result.current.attachments[0].id))
    act(() => result.current.clearAttachments())
    await act(async () => rejectDelete(new Error('old deletion failed')))
    expect(onError).not.toHaveBeenCalled()
  })

})

it.each([true, false])('移动会话重传本地文件，保留历史引用且旧上传不能写回：当前选中=%s', async selected => {
  vi.mocked(uploadAttachment).mockReset()
  const localFile = new File(['pdf'], 'local.pdf')
  const uploadingFile = new File(['md'], 'pending.md')
  const items: DraftAttachment[] = [
    { id: 'local', name: localFile.name, file: localFile, uploadProjectId: 'first', size: 3, kind: 'document', state: 'ready', progress: 100, attachment: { id: 'old-local', name: localFile.name, mime_type: 'application/pdf', size_bytes: 3 } },
    { id: 'pending', name: uploadingFile.name, file: uploadingFile, uploadProjectId: 'first', size: 2, kind: 'document', state: 'queued', progress: 0 },
    { id: 'reference', name: 'history.pdf', reference: true, size: 3, kind: 'document', state: 'ready', progress: 100, attachment: { id: 'history-file', name: 'history.pdf', mime_type: 'application/pdf', size_bytes: 3 } },
  ]
  const store = new Map([['thread:thread', items]])
  let completeSource: ((attachment: { id: string; name: string; mime_type: string; size_bytes: number }) => void) | undefined
  vi.mocked(uploadAttachment).mockImplementation((projectId, file) => projectId === 'first'
    ? new Promise(resolve => { completeSource = resolve })
    : Promise.resolve({ id: `second-${file.name}`, name: file.name, mime_type: 'application/pdf', size_bytes: file.size }))
  const current = renderHook(() => useAttachments('first', undefined, { key: selected ? 'thread:thread' : 'first:other', store }))
  const signal = vi.mocked(uploadAttachment).mock.calls[0]?.[2]
  current.unmount()
  if (selected) expect(signal.aborted).toBe(true)
  const target = renderHook(() => useAttachments('second', undefined, { key: 'thread:thread', store }))
  await act(async () => { completeSource?.({ id: 'old-upload', name: uploadingFile.name, mime_type: 'text/markdown', size_bytes: 2 }) })
  const destinationUploads = vi.mocked(uploadAttachment).mock.calls.filter(([projectId]) => projectId === 'second')
  expect(destinationUploads.map(([, file]) => file)).toEqual([localFile, uploadingFile])
  expect(target.result.current.attachments[2].attachment?.id).toBe('history-file')
  expect(target.result.current.attachments.slice(0, 2).map(item => item.attachment?.id)).toEqual(['second-local.pdf', 'second-pending.md'])
  target.unmount()
})

it('没有本地文件的非历史附件阻止跨项目移动，并保留现有草稿', () => {
  const items: DraftAttachment[] = [{ id: 'local', name: 'local.pdf', uploadProjectId: 'first', size: 3, kind: 'document', state: 'ready', progress: 100, attachment: { id: 'source-file', name: 'local.pdf', mime_type: 'application/pdf', size_bytes: 3 } }]
  const store = new Map([['thread:thread', items]])
  const current = renderHook(() => useAttachments('first', undefined, { key: 'thread:thread', store }))
  expect(() => current.result.current.validateProjectMove('thread:thread')).toThrow('附件的本地文件不可用，请重新选择后再移动会话')
  expect(store.get('thread:thread')).toEqual(items)
  current.unmount()
})

it('项目组件在同一次提交交接时，旧上传清理不能覆盖目标项目的附件记录', async () => {
  vi.mocked(uploadAttachment).mockReset()
  const file = new File(['pdf'], 'local.pdf')
  const items: DraftAttachment[] = [{ id: 'local', name: file.name, file, uploadProjectId: 'first', size: file.size, kind: 'document', state: 'queued', progress: 0 }]
  const store = new Map([['thread:thread', items]])
  let finishSource!: (value: { id: string; name: string; mime_type: string; size_bytes: number }) => void
  vi.mocked(uploadAttachment).mockImplementation(projectId => projectId === 'first'
    ? new Promise(resolve => { finishSource = resolve })
    : new Promise(() => {}))
  function Owner({ projectId }: { projectId: string }) {
    useAttachments(projectId, undefined, { key: 'thread:thread', store })
    return null
  }
  const view = render(createElement(Owner, { key: 'first', projectId: 'first' }))
  const sourceSignal = vi.mocked(uploadAttachment).mock.calls[0][2]
  view.rerender(createElement(Owner, { key: 'second', projectId: 'second' }))
  expect(sourceSignal.aborted).toBe(true)
  expect(store.get('thread:thread')?.[0]).toMatchObject({ uploadProjectId: 'second', state: 'uploading' })
  await act(async () => finishSource({ id: 'source-file', name: file.name, mime_type: 'application/pdf', size_bytes: file.size }))
  expect(store.get('thread:thread')?.[0]).toMatchObject({ uploadProjectId: 'second', state: 'uploading' })
  expect(store.get('thread:thread')?.[0].attachment).toBeUndefined()
  view.unmount()
})
