import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AttachmentList } from './AttachmentList'
import { AttachmentReferenceContext } from './context'
import { attachmentBlob } from './client'
import { MessageBlock, ToolCallBatch } from '../components/MessageBlock'
import type { Message } from '../../../types'

vi.mock('./client', () => ({ attachmentBlob: vi.fn() }))
const cat = {
  id: 'cat',
  name: '猫.png',
  mime_type: 'image/png',
  size_bytes: 4096,
}
const chart = { ...cat, id: 'chart', name: '图表.png' }
beforeEach(() => {
  vi.mocked(attachmentBlob)
    .mockReset()
    .mockResolvedValue(new Blob(['image']))
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:image'),
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  })
})

describe('无边图片和全屏预览', () => {
  it('图片加载失败时禁用引用和下载，重试成功后恢复操作', async () => {
    vi.mocked(attachmentBlob).mockRejectedValue(new Error('unavailable'))
    const reference = vi.fn()
    const user = userEvent.setup()
    render(
      <AttachmentReferenceContext.Provider value={reference}>
        <AttachmentList attachments={[cat]} />
      </AttachmentReferenceContext.Provider>,
    )
    const retry = await screen.findByRole('button', { name: '重试附件' })
    expect(retry).toBeEnabled()
    const quote = screen.getByRole('button', { name: '引用附件：猫.png' })
    const download = screen.getByRole('button', { name: '下载附件：猫.png' })
    expect(quote).toBeDisabled()
    expect(download).toBeDisabled()
    await user.click(quote)
    await user.click(download)
    expect(reference).not.toHaveBeenCalled()
    expect(attachmentBlob).toHaveBeenCalledTimes(1)
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['image']))
    await user.click(retry)
    await waitFor(() => expect(screen.getByRole('button', { name: '放大图片：猫.png' })).toBeEnabled())
    expect(screen.getByRole('button', { name: '引用附件：猫.png' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '下载附件：猫.png' })).toBeEnabled()
  })
  it('引用当前图片、查看信息、多图切换、缩放和关闭恢复焦点', async () => {
    const reference = vi.fn()
    const user = userEvent.setup()
    render(
      <AttachmentReferenceContext.Provider value={reference}>
        <AttachmentList attachments={[cat, chart]} />
      </AttachmentReferenceContext.Provider>,
    )
    const opener = screen.getByRole('button', { name: '放大图片：猫.png' })
    await waitFor(() => expect(opener).toBeEnabled())
    await user.click(opener)
    let dialog = screen.getByRole('dialog', { name: cat.name })
    expect(within(dialog).getByRole('img', { name: cat.name })).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: '图片信息' }))
    expect(within(dialog).getByText('image/png')).toBeVisible()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeVisible()
    expect(
      screen.queryByRole('complementary', { name: '图片信息' }),
    ).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: '原始尺寸' }))
    expect(
      within(dialog).getByRole('status', { name: '缩放比例' }),
    ).toHaveTextContent('100%')
    await user.click(within(dialog).getByRole('button', { name: '放大' }))
    expect(
      within(dialog).getByRole('status', { name: '缩放比例' }),
    ).toHaveTextContent('125%')
    const next = within(dialog).getByRole('button', { name: '下一张' })
    await user.click(next)
    expect(screen.getByRole('dialog')).toBe(dialog)
    dialog = screen.getByRole('dialog', { name: chart.name })
    expect(
      within(dialog).getByRole('button', { name: '下一张' }),
    ).toHaveAttribute('aria-disabled', 'true')
    await user.click(
      within(dialog).getByRole('button', { name: '引用附件：图表.png' }),
    )
    expect(reference).toHaveBeenCalledWith(chart)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })
  it('三张图片可连续按Enter切换，预览和导航焦点保持稳定', async () => {
    const user = userEvent.setup()
    render(
      <AttachmentList
        attachments={[cat, chart, { ...cat, id: 'third', name: '第三张.png' }]}
      />,
    )
    const opener = screen.getByRole('button', { name: '放大图片：猫.png' })
    await waitFor(() => expect(opener).toBeEnabled())
    await user.click(opener)
    const next = screen.getByRole('button', { name: '下一张' })
    next.focus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('dialog', { name: chart.name })).toBeVisible()
    expect(next).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('dialog', { name: '第三张.png' })).toBeVisible()
    expect(next).toHaveAttribute('aria-disabled', 'true')
  })
  it('原图与下载分别失败时，保留图片并在预览内重试', async () => {
    vi.mocked(attachmentBlob).mockImplementation(async (_id, variant) => {
      if (variant === 'original') throw new Error('offline')
      return new Blob(['preview'])
    })
    const user = userEvent.setup()
    render(<AttachmentList attachments={[cat]} />)
    const opener = screen.getByRole('button', { name: '放大图片：猫.png' })
    await waitFor(() => expect(opener).toBeEnabled())
    await user.click(opener)
    const dialog = screen.getByRole('dialog')
    await waitFor(() =>
      expect(
        within(dialog).getByText('原图暂时无法加载，仍可查看预览'),
      ).toBeVisible(),
    )
    expect(within(dialog).getByRole('img')).toBeVisible()
    await user.click(
      within(dialog).getByRole('button', { name: '下载附件：猫.png' }),
    )
    expect(
      await within(dialog).findByRole('button', { name: '重试下载' }),
    ).toBeVisible()
    vi.mocked(attachmentBlob).mockResolvedValue(new Blob(['original']))
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => undefined)
    await user.click(within(dialog).getByRole('button', { name: '重试下载' }))
    await waitFor(() => expect(click).toHaveBeenCalledOnce())
    await user.click(within(dialog).getByRole('button', { name: '重试原图' }))
    await waitFor(() =>
      expect(
        within(dialog).queryByText('原图暂时无法加载，仍可查看预览'),
      ).not.toBeInTheDocument(),
    )
    click.mockRestore()
  })
  it('损坏图片提供重新加载，非图片附件保留引用和下载', async () => {
    const user = userEvent.setup()
    render(
      <AttachmentList
        attachments={[
          cat,
          { ...chart, mime_type: 'application/pdf', name: '报告.pdf' },
        ]}
      />,
    )
    const img = await screen.findByRole('img', { name: cat.name })
    fireEvent.error(img)
    expect(
      screen.getByRole('button', { name: '放大图片：猫.png' }),
    ).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重试附件' }))
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: '放大图片：猫.png' }),
      ).toBeEnabled(),
    )
    expect(screen.getByText('报告.pdf')).toBeVisible()
    expect(
      screen.getByRole('button', { name: '下载附件：报告.pdf' }),
    ).toBeEnabled()
  })
})

describe('文件附件类型', () => {
  it('常见格式使用对应大图标，未知格式和无扩展名使用中性兜底', () => {
    const { container } = render(
      <AttachmentList
        attachments={[
          { ...cat, id: 'docx', name: '方案.docx', mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
          { ...cat, id: 'pdf', name: '报告.pdf', mime_type: 'application/pdf' },
          { ...cat, id: 'markdown', name: 'README.md', mime_type: 'text/markdown' },
          { ...cat, id: 'sheet', name: '报价.xlsx', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
          { ...cat, id: 'slides', name: '汇报.pptx', mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' },
          { ...cat, id: 'unknown', name: 'model.abcdefgh', mime_type: 'application/octet-stream', size_bytes: 2 * 1024 * 1024 },
          { ...cat, id: 'extensionless', name: 'LICENSE', mime_type: 'application/octet-stream' },
        ]}
      />,
    )

    expect(
      Array.from(container.querySelectorAll('.attachment-file-icon')).map(
        icon => icon.getAttribute('data-file-kind'),
      ),
    ).toEqual(['word', 'pdf', 'markdown', 'sheet', 'slides', 'unknown', 'unknown'])
    for (const label of ['DOCX', 'PDF', 'MD', 'XLSX', 'PPTX', 'ABCD', 'FILE'])
      expect(screen.getByText(label)).toBeVisible()
    expect(screen.getByText('2.0 MiB')).toBeVisible()
    expect(screen.getByRole('button', { name: '查看文件：model.abcdefgh' })).toBeEnabled()
  })

  it('未知格式打开统一预览器并保留下载路径', async () => {
    const user = userEvent.setup()
    render(
      <AttachmentList
        attachments={[
          { ...cat, id: 'unknown', name: 'model.bin', mime_type: 'application/octet-stream' },
        ]}
      />,
    )

    const opener = screen.getByRole('button', { name: '查看文件：model.bin' })
    await user.click(opener)
    const dialog = screen.getByRole('dialog', { name: 'model.bin' })
    expect(within(dialog).getByRole('heading', { name: '此格式无法在线预览' })).toBeVisible()
    expect(within(dialog).getByRole('button', { name: '下载文件' })).toBeEnabled()
    expect(attachmentBlob).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: '关闭对话框' }))
    expect(opener).toHaveFocus()
  })
})

const tool: Message = {
  id: 'tool',
  role: 'tool',
  content: '',
  createdAt: '',
  attachments: [cat],
  meta: {
    toolName: 'generate_image',
    params: '{"prompt":"cat"}',
    status: 'completed',
  },
}
describe('生成结果布局', () => {
  it('主工具及连续工具展开收起时保留同一图片，不显示空输出', async () => {
    const user = userEvent.setup()
    render(
      <ToolCallBatch
        messages={[tool, { ...tool, id: 'tool2', attachments: [chart] }]}
      />,
    )
    const img = await screen.findByRole('img', { name: cat.name })
    const details = screen.getAllByText('generate_image · cat')[0]
    await user.click(details)
    expect(screen.queryByText('输出')).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: cat.name })).toBe(img)
    await user.click(details)
    expect(screen.getByRole('img', { name: cat.name })).toBe(img)
  })
  it('子Agent工具及最终输出图片与文字详情保持独立', async () => {
    const user = userEvent.setup()
    render(
      <MessageBlock
        message={{
          id: 'agent',
          role: 'subagent',
          content: '',
          createdAt: '',
          attachments: [chart],
          meta: { agentName: 'designer', status: 'completed' },
        }}
        childTools={[tool]}
      />,
    )
    await user.click(screen.getByText('Task'))
    const img = await screen.findByRole('img', { name: cat.name })
    await user.click(screen.getByText('generate_image · cat'))
    expect(screen.getByRole('img', { name: cat.name })).toBe(img)
    expect(screen.getByRole('img', { name: chart.name })).toBeVisible()
    expect(screen.queryByText('输出')).not.toBeInTheDocument()
  })
})
