import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AttachmentList } from './AttachmentList'
import { DocumentAttachmentPreview } from './DocumentAttachmentPreview'
import { attachmentBlob } from './client'
import type { Attachment } from './content'
import { readOfficePreview } from './readOfficePreview'

vi.mock('./client', () => ({ attachmentBlob: vi.fn() }))
vi.mock('./readOfficePreview', () => ({ readOfficePreview: vi.fn() }))
const document = (mime = 'text/markdown', name = '门店月报.md'): Attachment => ({
  id: 'report', name, mime_type: mime, size_bytes: 50,
})
const docxMime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

beforeEach(() => {
  vi.mocked(attachmentBlob).mockReset().mockResolvedValue(new Blob(['# 九月营收\n\n增长 **10%**']))
  vi.mocked(readOfficePreview).mockReset()
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:document') })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
})
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

async function openDocument(attachment: Attachment) {
  const user = userEvent.setup()
  const view = render(<AttachmentList attachments={[attachment]} />)
  const opener = screen.getByRole('button', { name: `预览文档：${attachment.name}` })
  await user.click(opener)
  return { ...view, user, opener, dialog: screen.getByRole('dialog', { name: attachment.name }) }
}

describe('文档预览', () => {
  it('DOCX 保留标题和表格，净化脚本、外部图片、样式、事件和恶意链接', async () => {
    vi.mocked(readOfficePreview).mockResolvedValue({ kind: 'docx', html: '<h1>门店营收</h1><table><tr><td>42</td></tr></table><script>alert(1)</script><img src="https://tracker.invalid"><svg onload="alert(1)"></svg><p id="root" style="position:fixed" onclick="alert(1)">正文</p><a href="javascript:alert(1)">危险</a><a href="https://example.com">来源</a><iframe src="https://tracker.invalid"></iframe>' })
    const { dialog } = await openDocument(document(docxMime, '月报.docx'))
    expect(await within(dialog).findByRole('heading', { name: '门店营收' })).toBeVisible()
    expect(within(dialog).getByRole('cell', { name: '42' })).toBeVisible()
    expect(within(dialog).getByRole('region', { name: '文档预览' }).querySelector('script, img, svg, iframe, [onclick], [style], #root')).toBeNull()
    expect(within(dialog).getByText('危险')).not.toHaveAttribute('href')
    expect(within(dialog).getByRole('link', { name: '来源' })).toHaveAttribute('rel', 'noopener noreferrer')
    const [bytes, format, signal] = vi.mocked(readOfficePreview).mock.calls[0]!
    expect(new TextDecoder().decode(bytes)).toContain('九月营收')
    expect(format).toBe('docx')
    expect(signal.aborted).toBe(false)
  })
  it('声明为 PDF 的 HTML 文件不会获得可导航的预览 URL', async () => {
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['<html><script>alert(1)</script></html>']))
    const { dialog } = await openDocument(document('application/pdf', '伪装.pdf'))
    expect(await within(dialog).findByRole('button', { name: '重试预览' })).toBeVisible()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(dialog.querySelector('iframe')).toBeNull()
  })
  it('关闭未完成的读取后，晚到结果不创建资源', async () => {
    let release!: (blob: Blob) => void
    const response = new Promise<Blob>((resolve) => { release = resolve })
    vi.mocked(attachmentBlob).mockReturnValue(response)
    const { user, dialog } = await openDocument(document('application/pdf', '月报.pdf'))
    expect(within(dialog).getByRole('status')).toHaveTextContent('正在加载文档')
    const signal = vi.mocked(attachmentBlob).mock.calls[0]![2]!
    await user.click(within(dialog).getByRole('button', { name: '关闭对话框' }))
    expect(signal.aborted).toBe(true)
    await act(async () => { release(new Blob(['%PDF-1.4'])); await response })
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('更换附件后取消旧读取，不展示前一个文件的晚到正文', async () => {
    let release!: (blob: Blob) => void
    vi.mocked(attachmentBlob).mockReturnValueOnce(new Promise((resolve) => { release = resolve }))
    const view = render(<DocumentAttachmentPreview key="first" attachment={document()} onClose={vi.fn()} returnFocus={null} />)
    const signal = vi.mocked(attachmentBlob).mock.calls[0]![2]!
    view.rerender(<DocumentAttachmentPreview key="second" attachment={{ ...document(), id: 'second', name: '新月报.md' }} onClose={vi.fn()} returnFocus={null} />)
    expect(signal.aborted).toBe(true)
    expect(await screen.findByRole('heading', { name: '九月营收' })).toBeVisible()
    await act(async () => { release(new Blob(['# 旧文档'])) })
    expect(screen.queryByRole('heading', { name: '旧文档' })).not.toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
  })
  it('读取被拒绝后只显示本地错误，不把原始错误内容插入文档', async () => {
    vi.mocked(attachmentBlob).mockRejectedValue(new Error('<script>private storage address</script>'))
    const { dialog } = await openDocument(document())
    expect(await within(dialog).findByText('文档暂时无法打开，请重试')).toBeVisible()
    expect(dialog.textContent).not.toContain('private storage address')
    expect(dialog.querySelector('script')).toBeNull()
  })

})
