import axios from 'axios'
import { apiClient, type ApiAxiosRequestConfig } from '../../../api/shared/http'
import type { Attachment } from './content'

interface UploadPermit {
  attachment_id: string
  url: string
  fields: Record<string, string>
  expires_in: number
}

interface DownloadPermit {
  url: string
  expires_in: number
}

// 对象存储请求不经过业务认证拦截器，不附带 Studio 令牌或 Cookie
const storageClient = axios.create({ withCredentials: false })

export async function uploadAttachment(
  file: File,
  signal: AbortSignal,
  onProgress: (percent: number) => void,
): Promise<Attachment> {
  const request: ApiAxiosRequestConfig = {
    url: '/api/attachments/uploads',
    method: 'POST',
    data: { name: file.name, size_bytes: file.size },
    signal,
    suppressGlobalError: true,
  }
  const { data: permit } = await apiClient.request<UploadPermit>(request)
  const form = new FormData()
  for (const [key, value] of Object.entries(permit.fields)) form.append(key, value)
  form.append('file', file)
  await storageClient.request({
    url: permit.url,
    method: 'POST',
    adapter: 'xhr',
    data: form,
    signal,
    timeout: 60_000,
    onUploadProgress: ({ loaded, total }) =>
      onProgress(Math.min(99, Math.round((100 * loaded) / (total || file.size || 1)))),
  })
  const complete: ApiAxiosRequestConfig = {
    url: `/api/attachments/${encodeURIComponent(permit.attachment_id)}/complete`,
    method: 'POST',
    signal,
    timeout: 60_000,
    suppressGlobalError: true,
  }
  const { data: attachment } = await apiClient.request<Attachment>(complete)
  onProgress(100)
  return attachment
}

export async function attachmentBlob(
  id: string,
  variant: 'original' | 'preview',
  signal?: AbortSignal,
): Promise<Blob> {
  const request: ApiAxiosRequestConfig = {
    url: `/api/attachments/${encodeURIComponent(id)}/download-url`,
    params: { variant },
    signal,
    suppressGlobalError: true,
  }
  const { data: permit } = await apiClient.request<DownloadPermit>(request)
  return (await storageClient.request<Blob>({
    url: permit.url,
    responseType: 'blob',
    timeout: variant === 'preview' ? 30_000 : 60_000,
    signal,
  })).data
}

export async function removeDraftAttachment(id: string): Promise<void> {
  const config: ApiAxiosRequestConfig = {
    url: `/api/attachments/${encodeURIComponent(id)}`,
    method: 'DELETE',
    suppressGlobalError: true,
  }
  await apiClient.request<null>(config)
}
