import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import { DomFile } from '../../../test/setup'
import { apiClient } from '../../../api/shared/http'
import { attachmentBlob, uploadAttachment } from './client'

const { storageRequest } = vi.hoisted(() => ({ storageRequest: vi.fn() }))
vi.mock('axios', () => ({ default: { create: vi.fn(() => ({ request: storageRequest })) } }))
vi.mock('../../../api/shared/http', () => ({ apiClient: { request: vi.fn() } }))

beforeEach(() => {
  vi.mocked(apiClient.request).mockReset()
  storageRequest.mockReset()
})

describe('附件直传与下载', () => {
  it('先申请再直传，完成确认后才返回附件', async () => {
    const file = new DomFile(['text'], 'report.md')
    const signal = new AbortController().signal
    const attachment = { id: 'stored', name: file.name, mime_type: 'text/markdown', size_bytes: 4 }
    vi.mocked(apiClient.request)
      .mockResolvedValueOnce({ data: { attachment_id: 'stored', url: 'https://files.example/bucket', fields: { key: 'temporary', policy: 'signed' }, expires_in: 600 } })
      .mockResolvedValueOnce({ data: attachment })
    storageRequest.mockImplementationOnce(async config => {
      expect(apiClient.request).toHaveBeenCalledTimes(1)
      expect(config.data.get('file').name).toBe(file.name)
      expect(config.data.get('policy')).toBe('signed')
      expect(config).toMatchObject({ url: 'https://files.example/bucket', method: 'POST', signal })
      config.onUploadProgress({ loaded: 4, total: 4 })
      return { status: 204 }
    })
    const progress = vi.fn()
    expect(await uploadAttachment(file, signal, progress)).toEqual(attachment)
    expect(apiClient.request).toHaveBeenLastCalledWith(expect.objectContaining({ url: '/api/attachments/stored/complete', signal }))
    expect(progress.mock.calls).toEqual([[99], [100]])
    expect(axios.create).toHaveBeenCalledWith({ withCredentials: false })
  })

  it('直传失败不确认附件', async () => {
    vi.mocked(apiClient.request).mockResolvedValueOnce({ data: { attachment_id: 'a', url: 'https://files.example', fields: {} } })
    storageRequest.mockRejectedValueOnce(new Error('upload failed'))
    await expect(uploadAttachment(new DomFile(['a'], 'a.md'), new AbortController().signal, vi.fn())).rejects.toThrow('upload failed')
    expect(apiClient.request).toHaveBeenCalledTimes(1)
  })

  it.each([['preview', 30_000], ['original', 60_000]] as const)(
    '%s 使用后端返回的地址，读取保留超时和取消信号', async (variant, timeout) => {
      const blob = new Blob(['image'])
      vi.mocked(apiClient.request).mockResolvedValueOnce({ data: { url: 'https://files.example/signed', expires_in: 300 } })
      storageRequest.mockResolvedValueOnce({ data: blob })
      const signal = new AbortController().signal
      expect(await attachmentBlob('a/b', variant, signal)).toBe(blob)
      expect(apiClient.request).toHaveBeenCalledWith(expect.objectContaining({ url: '/api/attachments/a%2Fb/download-url', params: { variant }, signal }))
      expect(storageRequest).toHaveBeenCalledWith(expect.objectContaining({ url: 'https://files.example/signed', timeout, signal, responseType: 'blob' }))
    },
  )
})
