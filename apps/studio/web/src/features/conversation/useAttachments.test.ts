import { act, render, renderHook, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { removeDraftAttachment, uploadAttachment } from './attachments/client'
import { useAttachments, type DraftAttachment } from './useAttachments'

vi.mock('./attachments/client', () => ({
  uploadAttachment: vi.fn(),
  removeDraftAttachment: vi.fn().mockResolvedValue(undefined),
}))

describe('attachment uploads', () => {
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
