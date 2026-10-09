import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { runFixture } from '../../test/automationFixtures'
import { attachmentBlob } from '../conversation/attachments/client'
import { fetchRunDetail } from './api'
import { AutomationRunDialog } from './AutomationRunDialog'

vi.mock('./api', () => ({ fetchRunDetail: vi.fn() }))
vi.mock('../conversation/attachments/client', () => ({ attachmentBlob: vi.fn() }))

it('结果文件下载失败就地反馈，重试请求归属当前弹窗且关闭时取消', async () => {
  const run = runFixture()
  vi.mocked(fetchRunDetail).mockResolvedValue({ ...run, threadId: 'thread', runId: run.id, resultAvailable: true, messages: [], outputFiles: [{ id: 'report', name: 'report.md', mime_type: 'text/markdown', size_bytes: 10 }] })
  let release!: () => void
  const pending = new Promise<Blob>(resolve => { release = () => resolve(new Blob(['report'])) })
  vi.mocked(attachmentBlob).mockRejectedValueOnce(new Error('download failed')).mockReturnValueOnce(pending)
  const { unmount } = render(<AutomationRunDialog projectId="project-1" run={run} trigger={null} onToast={vi.fn()} onClose={vi.fn()} />)
  const file = await screen.findByRole('group', { name: 'report.md' })
  fireEvent.click(within(file).getByRole('button', { name: 'report.md' }))
  expect(await within(file).findByRole('alert')).toHaveTextContent('下载失败，请重试')
  fireEvent.click(within(file).getByRole('button', { name: '重试下载' }))
  expect(within(file).queryByRole('alert')).not.toBeInTheDocument()
  expect(within(file).getByRole('button', { name: 'report.md' })).toBeDisabled()
  expect(attachmentBlob).toHaveBeenCalledTimes(2)
  const signal = vi.mocked(attachmentBlob).mock.calls[1][2]
  unmount()
  expect(signal?.aborted).toBe(true)
  await act(async () => { release(); await pending })
})
