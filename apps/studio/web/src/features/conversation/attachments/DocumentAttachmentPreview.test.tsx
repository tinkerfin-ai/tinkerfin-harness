import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AttachmentList } from './AttachmentList'
import { DocumentAttachmentPreview } from './DocumentAttachmentPreview'
import { AttachmentReferenceContext } from './context'
import { attachmentBlob } from './client'
import { readOfficePreview } from './readOfficePreview'
import { DOCUMENT_PREVIEW_BYTES, DocumentPreviewError } from './documentPreview'
import type { Attachment } from './content'
import { LocaleContext } from '../../../i18n/LocaleContext'
import { translate, type ResolvedLanguage } from '../../../i18n/locale'

vi.mock('./client', () => ({ attachmentBlob: vi.fn() }))
vi.mock('./readOfficePreview', () => ({ readOfficePreview: vi.fn() }))
const document = (mime = 'text/markdown', name = '门店月报.md'): Attachment => ({
  id: 'report', name, mime_type: mime, size_bytes: 50,
})
const docxMime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const xlsxMime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

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
  it('经原件鉴权接口读取 Markdown，安全展示并关闭恢复焦点', async () => {
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['# 九月营收\n\n<script>alert(1)</script>\n\n![跟踪图片](https://tracker.invalid/a)\n\n[危险链接](javascript:alert(1))\n\n[参考](https://example.com/report)']))
    const { user, opener, dialog } = await openDocument(document())
    expect(await within(dialog).findByRole('heading', { name: '九月营收' })).toBeVisible()
    expect(attachmentBlob).toHaveBeenCalledWith('report', 'original', expect.any(AbortSignal))
    expect(dialog.querySelector('script, img')).toBeNull()
    expect(within(dialog).getByText('跟踪图片')).toBeVisible()
    expect(within(dialog).getByText('危险链接')).not.toHaveAttribute('href', expect.stringContaining('javascript:'))
    expect(within(dialog).getByRole('link', { name: '参考' })).toHaveAttribute('rel', 'noopener noreferrer')
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })
  it.each([new Uint8Array([0xff]), new TextEncoder().encode('bad\0file')])('拒绝不可用 UTF-8 或二进制文本并可重试', async (bytes) => {
    vi.mocked(attachmentBlob).mockResolvedValueOnce(new Blob([bytes]))
    const { user, dialog } = await openDocument(document())
    const retry = await within(dialog).findByRole('button', { name: '重试预览' })
    await user.click(retry)
    expect(await within(dialog).findByRole('heading', { name: '九月营收' })).toBeVisible()
    expect(attachmentBlob).toHaveBeenCalledTimes(2)
  })
  it('长 Markdown 明确标记预览截断，保留下载完整文档入口', async () => {
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['a'.repeat(100_000) + '不应展示的尾部']))
    const { dialog } = await openDocument(document())
    expect(await within(dialog).findByText('文档较长，仅预览前 10 万字符；下载可查看完整内容')).toBeVisible()
    expect(dialog.textContent).not.toContain('不应展示的尾部')
    expect(within(dialog).getByRole('button', { name: '下载附件：门店月报.md' })).toBeEnabled()
  })
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
  it('PDF 使用正确媒体类型的原件 Blob URL，关闭释放资源', async () => {
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['%PDF-1.4\nexample']))
    const { user, dialog } = await openDocument(document('application/pdf', '月报.pdf'))
    const frame = await within(dialog).findByTitle('PDF 预览：月报.pdf')
    expect(frame).toHaveAttribute('src', 'blob:document')
    expect(frame).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(vi.mocked(URL.createObjectURL).mock.calls[0]?.[0]).toMatchObject({ type: 'application/pdf' })
    expect(readOfficePreview).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: '关闭对话框' }))
    expect(URL.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:document')
  })
  it('声明为 PDF 的 HTML 文件不会获得可导航的预览 URL', async () => {
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['<html><script>alert(1)</script></html>']))
    const { dialog } = await openDocument(document('application/pdf', '伪装.pdf'))
    expect(await within(dialog).findByRole('button', { name: '重试预览' })).toBeVisible()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(dialog.querySelector('iframe')).toBeNull()
  })
  it('Excel 表格使用文本单元格并说明预览限制', async () => {
    vi.mocked(readOfficePreview).mockResolvedValue({ kind: 'xlsx', sheet: '营收', columns: ['A', 'B'], rows: [['<img src=x onerror=alert(1)>', '=WEBSERVICE("https://example.com")']], truncated: true })
    const { dialog } = await openDocument(document(xlsxMime, '月报.xlsx'))
    expect(await within(dialog).findByRole('cell', { name: '<img src=x onerror=alert(1)>' })).toBeVisible()
    expect(dialog.querySelector('img, a')).toBeNull()
    expect(within(dialog).getByText(/最多显示 100 行、20 列/)).toBeVisible()
    expect(within(dialog).getByText(/仅预览第一个工作表的已保存值，不计算公式/)).toBeVisible()
  })
  it('DOCX 表格有独立键盘滚动入口，切语言保留原文且不重新读取', async () => {
    vi.mocked(readOfficePreview).mockResolvedValue({ kind: 'docx', html: '<p>完整的长段落说明保持正常换行</p><table><tr><td>4800</td><td>30.0%</td><td><table><tr><td>57600</td></tr></table></td></tr></table>' })
    const attachment = document(docxMime, '门店简报.docx')
    const renderInLocale = (locale: ResolvedLanguage) => <LocaleContext.Provider value={{
      locale, preference: locale, setPreference: vi.fn(), t: (key, params) => translate(locale, key, params),
    }}><AttachmentList attachments={[attachment]} /></LocaleContext.Provider>
    const view = render(renderInLocale('zh-CN'))
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '预览文档：门店简报.docx' }))
    const regions = await screen.findAllByRole('region', { name: '可横向滚动的表格' })
    expect(regions).toHaveLength(2)
    for (const region of regions) expect(region).toHaveAttribute('tabindex', '0')
    expect(within(regions[0]!).getByRole('cell', { name: '4800' })).toHaveTextContent('4800')
    expect(screen.getByRole('cell', { name: '30.0%' })).toHaveTextContent('30.0%')
    expect(screen.getByText('完整的长段落说明保持正常换行')).toBeVisible()
    view.rerender(renderInLocale('en'))
    expect(screen.getAllByRole('region', { name: 'Horizontally scrollable table' })).toHaveLength(2)
    expect(screen.queryByRole('region', { name: '可横向滚动的表格' })).not.toBeInTheDocument()
    expect(attachmentBlob).toHaveBeenCalledTimes(1)
    expect(readOfficePreview).toHaveBeenCalledTimes(1)
  })

  it('空工作表有明确提示', async () => {
    vi.mocked(readOfficePreview).mockResolvedValue({ kind: 'xlsx', sheet: '空表', columns: [], rows: [], truncated: false })
    const { dialog } = await openDocument(document(xlsxMime, '空表.xlsx'))
    expect(await within(dialog).findByText('此工作表为空')).toBeVisible()
  })
  it.each(['metadata', 'actual'] as const)('%s 大小超限仍允许下载，不启动解析', async (sizeSource) => {
    const attachment = document(docxMime, '大文档.docx')
    if (sizeSource === 'metadata') attachment.size_bytes = DOCUMENT_PREVIEW_BYTES + 1
    else vi.mocked(attachmentBlob).mockResolvedValue(new Blob([new Uint8Array(DOCUMENT_PREVIEW_BYTES + 1)]))
    const { dialog } = await openDocument(attachment)
    expect(await within(dialog).findByText('文档超出预览限制，请下载查看')).toBeVisible()
    expect(readOfficePreview).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('button', { name: '下载附件：大文档.docx' })).toBeEnabled()
  })
  it('解析超时保留错误提示和重试入口', async () => {
    vi.mocked(readOfficePreview).mockRejectedValue(new DocumentPreviewError('timeout'))
    const { dialog } = await openDocument(document(docxMime, '月报.docx'))
    expect(await within(dialog).findByText('文档解析超时，请下载查看或重试')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: '重试预览' })).toBeEnabled()
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
  it('关闭解析中的文档会中止解析，晚到的 HTML 不再显示', async () => {
    let release!: (result: { kind: 'docx'; html: string }) => void
    const response = new Promise<{ kind: 'docx'; html: string }>((resolve) => { release = resolve })
    vi.mocked(readOfficePreview).mockReturnValue(response)
    const { user, dialog } = await openDocument(document(docxMime, '月报.docx'))
    const signal = vi.mocked(readOfficePreview).mock.calls[0]![2]
    await user.click(within(dialog).getByRole('button', { name: '关闭对话框' }))
    expect(signal.aborted).toBe(true)
    await act(async () => { release({ kind: 'docx', html: '<h1>晚到内容</h1>' }); await response })
    expect(screen.queryByText('晚到内容')).not.toBeInTheDocument()
  })
  it('下载失败可再次下载原件，引用关闭预览并保留文件身份', async () => {
    const reference = vi.fn()
    const user = userEvent.setup()
    const attachment = document()
    render(<AttachmentReferenceContext.Provider value={reference}><AttachmentList attachments={[attachment]} /></AttachmentReferenceContext.Provider>)
    await user.click(screen.getByRole('button', { name: '预览文档：门店月报.md' }))
    const dialog = screen.getByRole('dialog')
    await within(dialog).findByRole('heading', { name: '九月营收' })
    vi.mocked(attachmentBlob).mockRejectedValueOnce(new Error('offline'))
    await user.click(within(dialog).getByRole('button', { name: '下载附件：门店月报.md' }))
    expect(await within(dialog).findByText('下载失败，请重试')).toBeVisible()
    let downloadedName: string | undefined
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { downloadedName = this.download })
    await user.click(within(dialog).getByRole('button', { name: '下载附件：门店月报.md' }))
    expect(click).toHaveBeenCalledOnce()
    expect(downloadedName).toBe(attachment.name)
    await user.click(within(dialog).getByRole('button', { name: '引用附件：门店月报.md' }))
    expect(reference).toHaveBeenCalledExactlyOnceWith(attachment)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(URL.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:document')
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
  it.each(['markdown', 'docx'] as const)('%s 没有正文时提供空内容提示', async (format) => {
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['']))
    vi.mocked(readOfficePreview).mockResolvedValue({ kind: 'docx', html: '' })
    const { dialog } = await openDocument(format === 'markdown' ? document() : document(docxMime, '空文档.docx'))
    expect(await within(dialog).findByText(format === 'markdown' ? '此文档为空' : '此文档没有可预览的文字')).toBeVisible()
  })
  it('读取被拒绝后只显示本地错误，不把原始错误内容插入文档', async () => {
    vi.mocked(attachmentBlob).mockRejectedValue(new Error('<script>private storage address</script>'))
    const { dialog } = await openDocument(document())
    expect(await within(dialog).findByText('文档暂时无法打开，请重试')).toBeVisible()
    expect(dialog.textContent).not.toContain('private storage address')
    expect(dialog.querySelector('script')).toBeNull()
  })

})
