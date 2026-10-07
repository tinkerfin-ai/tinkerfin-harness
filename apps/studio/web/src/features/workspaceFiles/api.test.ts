import { beforeEach, expect, it, vi } from 'vitest'
import { requestJson } from '../../api/shared/http'
import { readWorkspaceDirectory, readWorkspaceFileInfo, readWorkspacePreview } from './api'

vi.mock('../../api/shared/http', async importOriginal => ({ ...await importOriginal<typeof import('../../api/shared/http')>(), requestJson: vi.fn() }))
const file = { path: '/script.py', name: 'script.py', kind: 'file', sizeBytes: 7, modifiedAt: '2030-01-01T00:00:00Z', etag: 'etag' }
const signal = new AbortController().signal
beforeEach(() => vi.resetAllMocks())

it('项目与路径分别编码，源码按文本交付且保留截断状态', async () => {
  vi.mocked(requestJson).mockResolvedValue({ kind: 'text', file, text: '<script>alert(1)</script>', truncated: true })
  expect(await readWorkspacePreview('project/a', '/script.py', signal)).toMatchObject({ text: '<script>alert(1)</script>', truncated: true })
  expect(requestJson).toHaveBeenCalledWith('/api/projects/project%2Fa/workspace/preview?path=%2Fscript.py', { signal, suppressGlobalError: true })
})

it('拒绝非直接子项、错误文件身份及超出预览限制的响应', async () => {
  vi.mocked(requestJson).mockResolvedValue({ state: 'ready', path: '/', entries: [{ ...file, path: '/nested/script.py' }], nextCursor: null })
  await expect(readWorkspaceDirectory('project', '/', null, signal)).rejects.toThrow('接口返回格式不合法')
  vi.mocked(requestJson).mockResolvedValue(file)
  await expect(readWorkspaceFileInfo('project', '/other.py', signal)).rejects.toThrow('接口返回格式不合法')
  vi.mocked(requestJson).mockResolvedValue({ kind: 'text', file, text: 'x'.repeat(102401), truncated: true })
  await expect(readWorkspacePreview('project', '/script.py', signal)).rejects.toThrow('接口返回格式不合法')
})
