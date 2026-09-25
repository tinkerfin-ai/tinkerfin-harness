import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from './Composer'
import type { DraftAttachment } from '../useAttachments'

const composerChromeProps = () => ({
  modelControl: <button type="button">测试模型</button>,
  planActive: false,
  attachments: [],
  onExitPlan: vi.fn(),
  onChooseModel: vi.fn(),
  onAddAttachments: vi.fn(),
  onRemoveAttachment: vi.fn(),
})

describe('Composer', () => {
  it.each([
    '当前模型不支持图片，请切换模型或移除图片',
    '当前模型的图片能力未确认，请切换模型或移除图片',
  ])('显示图片发送受阻原因并关联输入和发送按钮：%s', (reason) => {
    const attachment: DraftAttachment = {
      id: 'image', name: '截图.png', kind: 'image', size: 3, state: 'ready', progress: 100,
    }
    const props = {
      ...composerChromeProps(), value: '保留正文', isRunning: false, attachments: [attachment],
      onChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(),
    }
    const { rerender } = render(<Composer {...props} attachmentDisabledReason={reason} />)
    const notice = screen.getByRole('status')
    const input = screen.getByRole('textbox', { name: '消息输入' })
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(notice).toHaveTextContent(reason)
    expect(within(notice).queryByRole('button')).not.toBeInTheDocument()
    expect(input).toHaveAccessibleDescription(reason)
    expect(send).toHaveAccessibleDescription(reason)
    expect(send).toBeDisabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSend).not.toHaveBeenCalled()
    expect(input).toHaveValue('保留正文')

    rerender(<Composer {...props} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(input).not.toHaveAccessibleDescription()
    expect(send).not.toHaveAccessibleDescription()
    expect(send).toBeEnabled()
    expect(screen.getByRole('group', { name: '截图.png' })).toBeVisible()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSend).toHaveBeenCalledOnce()
  })

  it('压缩命令不发送附件，因此不显示图片阻塞提示', () => {
    render(<Composer {...composerChromeProps()} value="/compact 后续说明" isRunning={false}
      attachments={[{ id: 'image', name: '截图.png', kind: 'image', size: 3, state: 'ready', progress: 100 }]}
      attachmentDisabledReason="当前模型不支持图片，请切换模型或移除图片"
      onChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} onCompact={vi.fn(() => true)} />)
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '消息输入' })).not.toHaveAccessibleDescription()
  })

  it.each(['queued', 'uploading', 'error'] as const)('附件为 %s 时禁用发送并阻止 Enter，全部就绪后恢复', (state) => {
    const onSend = vi.fn()
    const ready: DraftAttachment = {
      id: 'ready', name: '已上传.pdf', kind: 'document', size: 3,
      state: 'ready', progress: 100,
      attachment: { id: 'stored', name: '已上传.pdf', size_bytes: 3, mime_type: 'application/pdf' },
    }
    const pending: DraftAttachment = { ...ready, id: 'pending', name: '待处理.pdf', state, progress: 25, attachment: undefined }
    const props = {
      ...composerChromeProps(), value: '保留正文', isRunning: false,
      onChange: vi.fn(), onSend, onStop: vi.fn(),
    }
    const { rerender } = render(<Composer {...props} attachments={[ready, pending]} />)
    const send = screen.getByRole('button', { name: '发送消息' })
    const input = screen.getByRole('textbox', { name: '消息输入' })
    expect(send).toBeDisabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    expect(input).toHaveValue('保留正文')
    rerender(<Composer {...props} attachments={[ready, { ...pending, state: 'ready', attachment: ready.attachment }]} />)
    expect(send).toBeEnabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(2)
  })

  it('失败卡片显示明确状态，支持重试和移除，移除后可发送正文', () => {
    const failed: DraftAttachment = {
      id: 'failed', name: '报告.pdf', kind: 'document', size: 3,
      state: 'error', progress: 0, error: '网络请求失败，请稍后重试',
    }
    const onRetryAttachment = vi.fn()
    const onRemoveAttachment = vi.fn()
    const props = {
      ...composerChromeProps(), value: '保留正文', isRunning: false,
      onChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(), onRetryAttachment, onRemoveAttachment,
    }
    const { rerender } = render(<Composer {...props} attachments={[failed]} />)
    const card = screen.getByRole('group', { name: '报告.pdf' })
    expect(within(card).getByText('PDF')).toBeInTheDocument()
    expect(within(card).getByRole('status')).toHaveTextContent('上传失败')
    expect(within(card).getByRole('status')).toHaveAttribute('title', failed.error)
    fireEvent.click(within(card).getByRole('button', { name: '重试附件：报告.pdf' }))
    expect(onRetryAttachment).toHaveBeenCalledWith('failed')
    rerender(<Composer {...props} attachments={[{ ...failed, state: 'uploading', progress: 42 }]} />)
    expect(card).toHaveAttribute('aria-busy', 'true')
    expect(within(card).getByRole('status')).toHaveTextContent('上传中 42%')
    expect(within(card).queryByText('上传失败')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    rerender(<Composer {...props} attachments={[failed]} />)
    fireEvent.click(within(card).getByRole('button', { name: '移除附件：报告.pdf' }))
    expect(onRemoveAttachment).toHaveBeenCalledWith('failed')
    rerender(<Composer {...props} attachments={[]} />)
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('保留正文')
  })

  it('按传入控件显示返回底部和轨迹入口，移除后不再提供入口', () => {
    const props = {
      ...composerChromeProps(), value: '', isRunning: false,
      onChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(),
    }
    const { rerender } = render(
      <Composer
        {...props}
        scrollToBottomControl={<button type="button">回到底部</button>}
        taskTraceControl={<button type="button">任务轨迹 2</button>}
      />,
    )
    expect(screen.getByRole('button', { name: '回到底部' })).toBeVisible()
    expect(screen.getByRole('button', { name: '任务轨迹 2' })).toBeVisible()
    rerender(<Composer {...props} />)
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '任务轨迹 2' })).not.toBeInTheDocument()
  })

  it('接管时隐藏输入控件，结束后恢复草稿和焦点', () => {
    const animation = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    const { rerender } = render(
      <Composer
        {...composerChromeProps()}
        takeover={<section aria-label="澄清接管">等待回答</section>}
        value="保留的草稿"
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const input = screen.getByLabelText('消息输入')
    const note = screen.getByText('TinkerFin 可能会犯错，请核对重要信息')
    expect(screen.queryByRole('textbox', { name: '消息输入' })).not.toBeInTheDocument()
    expect(input).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '澄清接管' })).toBeVisible()
    expect(note).toBeVisible()

    rerender(
      <Composer
        {...composerChromeProps()}
        value="保留的草稿"
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    expect(screen.getByRole('textbox', { name: '消息输入' })).toBe(input)
    expect(input).toHaveFocus()
    expect(input).toHaveValue('保留的草稿')
    expect(screen.getByText('TinkerFin 可能会犯错，请核对重要信息')).toBe(note)
    animation.mockRestore()
  })

  it('空草稿显示输入提示并禁用发送', () => {
    render(
      <Composer
        {...composerChromeProps()}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByPlaceholderText('给 TinkerFin 发消息')).toBeVisible()
    const sendButton = screen.getByRole('button', { name: '发送消息' })
    expect(sendButton).toBeDisabled()
  })

  it('提供命令、附件、权限和模型入口，并支持关闭 Plan', () => {
    const onExitPlan = vi.fn()
    render(
      <Composer
        {...composerChromeProps()}
        modelControl={<button type="button">GPT-5.5</button>}
        accessControl={<button type="button">选择访问权限</button>}
        planActive
        onExitPlan={onExitPlan}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    for (const name of ['打开命令和技能', '添加本地附件', '选择访问权限', 'GPT-5.5']) {
      expect(screen.getByRole('button', { name })).toBeEnabled()
    }
    const planChip = screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })
    planChip.focus()
    fireEvent.click(planChip)
    expect(onExitPlan).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveFocus()
  })

  it('shows ready attachments and allows removing them before sending', () => {
    const onAddAttachments = vi.fn()
    const onRemoveAttachment = vi.fn()
    const attachment = {
      id: 'attachment-1',
      file: new File(['pdf'], 'brief.pdf', { type: 'application/pdf' }),
      kind: 'document' as const, name: 'brief.pdf', size: 3, state: 'ready' as const, progress: 100,
    }
    const { container } = render(
      <Composer
        {...composerChromeProps()}
        attachments={[attachment]}
        onAddAttachments={onAddAttachments}
        onRemoveAttachment={onRemoveAttachment}
        value="正文"
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('brief.pdf')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '移除附件：brief.pdf' }))
    expect(onRemoveAttachment).toHaveBeenCalledWith('attachment-1')

    const fileInput = container.querySelector('input[type="file"]')
    if (!(fileInput instanceof HTMLInputElement)) throw new Error('missing attachment input')
    const image = new File(['image'], 'chart.png', { type: 'image/png' })
    fireEvent.change(fileInput, { target: { files: [image] } })
    expect(onAddAttachments).toHaveBeenCalledWith([image])
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('supports macOS Control+U without affecting Command+U', () => {
    const platform = vi.spyOn(window.navigator, 'platform', 'get').mockReturnValue('MacIntel')
    try {
      function ComposerHarness() {
        const [value, setValue] = useState('第一行\n第二行内容')
        return (
          <Composer
            {...composerChromeProps()}
            value={value}
            isRunning={false}
            onChange={setValue}
            onSend={vi.fn()}
            onStop={vi.fn()}
          />
        )
      }
      render(<ComposerHarness />)
      const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
      input.focus()
      input.setSelectionRange(input.value.length, input.value.length)

      fireEvent.keyDown(input, { key: 'u', code: 'KeyU', ctrlKey: true })

      expect(input).toHaveValue('第一行\n')
      expect(input.selectionStart).toBe('第一行\n'.length)

      fireEvent.change(input, { target: { value: '/plan 任务' } })
      input.setSelectionRange(3, 3)
      fireEvent.keyDown(input, { key: 'u', code: 'KeyU', ctrlKey: true })
      expect(input).toHaveValue('任务')

      fireEvent.change(input, { target: { value: '保留内容', selectionStart: 4 } })
      fireEvent.keyDown(input, { key: 'u', code: 'KeyU', metaKey: true })
      expect(input).toHaveValue('保留内容')
    } finally {
      platform.mockRestore()
    }
  })

  it('does not override Control+U outside macOS or during IME composition', () => {
    const platform = vi.spyOn(window.navigator, 'platform', 'get')
    try {
      function ComposerHarness() {
        const [value, setValue] = useState('保留内容')
        return (
          <Composer
            {...composerChromeProps()}
            value={value}
            isRunning={false}
            onChange={setValue}
            onSend={vi.fn()}
            onStop={vi.fn()}
          />
        )
      }
      const view = render(<ComposerHarness />)
      const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
      input.focus()
      input.setSelectionRange(input.value.length, input.value.length)

      platform.mockReturnValue('Win32')
      fireEvent.keyDown(input, { key: 'u', code: 'KeyU', ctrlKey: true })
      expect(input).toHaveValue('保留内容')

      platform.mockReturnValue('MacIntel')
      fireEvent.keyDown(input, {
        key: 'u',
        code: 'KeyU',
        ctrlKey: true,
        isComposing: true,
      })
      expect(input).toHaveValue('保留内容')
      view.unmount()
    } finally {
      platform.mockRestore()
    }
  })

  it('keeps a leading Slash insertion at the caret and cancels only that token', async () => {
    function ComposerHarness() {
      const [value, setValue] = useState('已有内容')
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      )
    }
    render(<ComposerHarness />)
    const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
    input.focus()
    input.setSelectionRange(0, 0)
    fireEvent.select(input)

    fireEvent.change(input, { target: { value: '/已有内容', selectionStart: 1 } })
    expect(input).toHaveValue('/已有内容')
    expect(input.selectionStart).toBe(1)
    expect(screen.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()

    fireEvent.change(input, { target: { value: '/x已有内容', selectionStart: 2 } })
    expect(input).toHaveValue('/已有内容')
    await waitFor(() => expect(input.selectionStart).toBe(1))

    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('已有内容')
    expect(input.selectionStart).toBe(0)
  })

  it('keeps wheel input inside the composer even when the input does not overflow', () => {
    const outerWheel = vi.fn()
    render(<div onWheel={outerWheel}>
      <Composer {...composerChromeProps()} value="" isRunning={false} onChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />
    </div>)
    const input = screen.getByLabelText('消息输入')
    fireEvent.wheel(input, { deltaY: -120 })
    expect(outerWheel).not.toHaveBeenCalled()
    fireEvent.wheel(input, { deltaY: 120 })
    expect(outerWheel).not.toHaveBeenCalled()
  })

  it('运行期间提供停止操作', () => {
    const onStop = vi.fn()
    render(
      <Composer
        {...composerChromeProps()}
        value="正在发送"
        isRunning
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={onStop}
      />,
    )

    const stopButton = screen.getByRole('button', { name: '停止任务' })
    fireEvent.click(stopButton)
    expect(onStop).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: '发送消息' })).not.toBeInTheDocument()
  })

  it.each([
    ['composition state', { isComposing: true }],
    ['IME compatibility key code', { keyCode: 229 }],
  ])('does not send while Enter confirms an IME %s', (_name, nativeFields) => {
    const onSend = vi.fn()
    render(
      <Composer
        {...composerChromeProps()}
        value="拼音输入"
        isRunning={false}
        onChange={vi.fn()}
        onSend={onSend}
        onStop={vi.fn()}
      />,
    )

    fireEvent.keyDown(screen.getByLabelText('消息输入'), {
      key: 'Enter',
      ...nativeFields,
    })

    expect(onSend).not.toHaveBeenCalled()
  })

  it('sends with Enter while leaving Shift+Enter to native multiline editing', () => {
    const onSend = vi.fn()
    render(
      <Composer
        {...composerChromeProps()}
        value="可发送内容"
        isRunning={false}
        onChange={vi.fn()}
        onSend={onSend}
        onStop={vi.fn()}
      />,
    )
    const input = screen.getByLabelText('消息输入')

    expect(fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })).toBe(true)
    expect(onSend).not.toHaveBeenCalled()
    expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(false)
    expect(onSend).toHaveBeenCalledOnce()
  })

  it('disables input and send while the selected history is hydrating', () => {
    render(
      <Composer
        {...composerChromeProps()}
        value="暂存内容"
        isRunning={false}
        isHydrating
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('消息输入')).toBeDisabled()
    expect(screen.getByPlaceholderText('正在加载会话…')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '打开命令和技能' })).toBeDisabled()
  })

  it('加号开关命令菜单并支持 Escape，保留草稿和输入焦点且不显示提示', () => {
    render(
      <Composer
        {...composerChromeProps()}
        value="已有内容"
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    const trigger = screen.getByRole('button', { name: '打开命令和技能' })
    const input = screen.getByRole('textbox', { name: '消息输入' })
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()
    expect(input).toHaveValue('已有内容')
    expect(input).toHaveFocus()
    expect(screen.queryByRole('tooltip', { name: '打开命令和技能', hidden: true })).not.toBeInTheDocument()

    fireEvent.keyDown(input, { key: 'Escape' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(input).toHaveValue('已有内容')
    expect(input).toHaveFocus()

    fireEvent.click(trigger)
    fireEvent.click(trigger)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(input).toHaveValue('已有内容')
  })

  it.each(['已有内容', '/plan 已有内容'])('加号菜单选择 Plan 后保留正文且只插入一次命令：%s', (initialValue) => {
    const onSend = vi.fn()
    function ComposerHarness() {
      const [value, setValue] = useState(initialValue)
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={onSend}
          onStop={vi.fn()}
        />
      )
    }
    render(<ComposerHarness />)
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    const input = screen.getByRole('textbox', { name: '消息输入' }) as HTMLTextAreaElement
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('/plan 已有内容')
    expect(input.selectionStart).toBe(6)
    expect(input).toHaveFocus()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(onSend).not.toHaveBeenCalled()
  })

  it('显示四条指令及数量，Plan 与模型选择可用', () => {
    const onChange = vi.fn()
    render(
      <Composer
        {...composerChromeProps()}
        value="/"
        isRunning={false}
        onChange={onChange}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const menu = screen.getByRole('listbox', { name: '命令和技能建议' })
    const commandGroup = within(menu).getByRole('group', { name: '指令' })
    const skillGroup = within(menu).getByRole('group', { name: '技能' })
    expect(within(commandGroup).getByText('指令（4）')).toBeVisible()
    expect(within(commandGroup).getAllByRole('option')).toHaveLength(4)
    expect(within(commandGroup).queryByRole('option', { name: /permission/ })).not.toBeInTheDocument()
    expect(within(skillGroup).getAllByRole('option')).toHaveLength(1)

    const plan = within(commandGroup).getByRole('option', { name: /plan 进入 Plan 模式/ })
    const model = within(commandGroup).getByRole('option', { name: /model 选择本会话使用的模型/ })
    const disabledOptions = within(menu).getAllByRole('option').filter((option) => option !== plan && option !== model)
    expect(plan).toBeEnabled()
    expect(model).toBeEnabled()
    disabledOptions.forEach((option) => expect(option).toBeDisabled())

    fireEvent.click(within(commandGroup).getByRole('option', { name: /compact/ }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.click(plan)
    expect(onChange).toHaveBeenCalledWith('/plan ')
  })

  it('从加号选择模型时关闭指令菜单，保留草稿并交给宿主打开模型选择', () => {
    const onChooseModel = vi.fn()
    const onChange = vi.fn()
    const onSend = vi.fn()
    render(<Composer {...composerChromeProps()} value="保留正文" isRunning={false}
      onChange={onChange} onSend={onSend} onStop={vi.fn()} onChooseModel={onChooseModel} />)
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    fireEvent.click(screen.getByRole('option', { name: /model 选择本会话使用的模型/ }))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('保留正文')
    expect(onChooseModel).toHaveBeenCalledOnce()
    expect(onChange).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
  })

  it.each(['Enter', 'Tab'])('输入模型指令后按 %s 只移除指令并保留后方正文', (key) => {
    const onChooseModel = vi.fn()
    const onSend = vi.fn()
    function ComposerHarness() {
      const [value, setValue] = useState('保留正文')
      return <Composer {...composerChromeProps()} value={value} isRunning={false}
        onChange={setValue} onSend={onSend} onStop={vi.fn()} onChooseModel={onChooseModel} />
    }
    render(<ComposerHarness />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    fireEvent.change(input, { target: { value: '/model保留正文', selectionStart: 6 } })
    expect(screen.getByText('指令（1）')).toBeVisible()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    fireEvent.keyDown(input, { key })
    expect(input).toHaveValue('保留正文')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(onChooseModel).toHaveBeenCalledOnce()
    expect(onSend).not.toHaveBeenCalled()
  })

  it('rejects unavailable slash commands and keeps incomplete prefixes non-submittable', () => {
    function ComposerHarness() {
      const [value, setValue] = useState('')
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      )
    }
    render(<ComposerHarness />)
    const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
    const send = screen.getByRole('button', { name: '发送消息' })

    fireEvent.change(input, { target: { value: '/p', selectionStart: 2 } })
    expect(input).toHaveValue('/p')
    expect(send).toBeDisabled()

    fireEvent.change(input, { target: { value: '/px', selectionStart: 3 } })
    expect(input).toHaveValue('/p')
    fireEvent.change(input, { target: { value: '/export', selectionStart: 7 } })
    expect(input).toHaveValue('/p')

    fireEvent.change(input, { target: { value: '/plan', selectionStart: 5 } })
    expect(input).toHaveValue('/plan')
    expect(send).toBeDisabled()
  })

  it.each(['/plan', '/plan ', '  /plan \n\t'])('Plan 任务正文为空时禁用发送并阻止 Enter：%j', (value) => {
    const onSend = vi.fn()
    const props = {
      ...composerChromeProps(), value, isRunning: false,
      onChange: vi.fn(), onSend, onStop: vi.fn(),
    }
    const { rerender } = render(<Composer {...props} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeDisabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<Composer {...props} value="/plan 制定方案" />)
    expect(send).toBeEnabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(2)
  })

  it('已就绪附件可以单独发送，Plan 指令仍需要任务正文', () => {
    const ready: DraftAttachment = {
      id: 'ready', name: '资料.pdf', kind: 'document', size: 3,
      state: 'ready', progress: 100,
      attachment: { id: 'stored', name: '资料.pdf', size_bytes: 3, mime_type: 'application/pdf' },
    }
    const onSend = vi.fn()
    const props = {
      ...composerChromeProps(), attachments: [ready], isRunning: false,
      onChange: vi.fn(), onSend, onStop: vi.fn(),
    }
    const { rerender } = render(<Composer {...props} value="/plan " />)
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeDisabled()
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息输入' }), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<Composer {...props} value="" />)
    expect(send).toBeEnabled()
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledOnce()
  })

  it.each(['/plan ', '/plan'])('removes the complete %s command with one Backspace', (initialValue) => {
    function ComposerHarness() {
      const [value, setValue] = useState(initialValue)
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      )
    }
    render(<ComposerHarness />)
    const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
    input.focus()
    input.setSelectionRange(initialValue.length, initialValue.length)
    fireEvent.keyDown(input, { key: 'Backspace' })

    expect(input).toHaveValue('')
    expect(input.selectionStart).toBe(0)
    expect(screen.queryByText('/pla')).not.toBeInTheDocument()
  })

  it.each(['Enter', 'Tab'])('selects Plan with %s, keeps focus, and decorates the claim', (key) => {
    const onSend = vi.fn()
    function ComposerHarness() {
      const [value, setValue] = useState('')
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={onSend}
          onStop={vi.fn()}
        />
      )
    }
    render(<ComposerHarness />)
    const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
    input.focus()
    fireEvent.change(input, { target: { value: '/', selectionStart: 1 } })

    expect(screen.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()
    expect(input).toHaveAttribute('aria-activedescendant', expect.stringContaining('command-plan'))
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-activedescendant', expect.stringContaining('command-model'))
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    fireEvent.keyDown(input, { key })

    expect(input).toHaveValue('/plan ')
    expect(input.selectionStart).toBe(6)
    expect(input).toHaveFocus()
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox', { name: '命令和技能建议' })).not.toBeInTheDocument()
    expect(screen.getByText('描述你的任务以生成计划')).toBeInTheDocument()
  })

  it('cancels both the slash trigger and suggestions with Escape', () => {
    function ComposerHarness() {
      const [value, setValue] = useState('/')
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      )
    }
    render(<ComposerHarness />)

    const input = screen.getByLabelText('消息输入')
    input.focus()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox', { name: '命令和技能建议' })).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    expect(input).toHaveFocus()
  })

  it('keeps menu scrolling inside the popup and restores input focus after a blank outside pointer', async () => {
    const outerWheel = vi.fn()
    function ComposerHarness() {
      const [value, setValue] = useState('/')
      return (
        <Composer
          {...composerChromeProps()}
          value={value}
          isRunning={false}
          onChange={setValue}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      )
    }
    render(<div onWheel={outerWheel}><ComposerHarness /></div>)

    const menu = screen.getByRole('listbox', { name: '命令和技能建议' })
    const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
    input.focus()
    input.setSelectionRange(1, 1)
    fireEvent.select(input)
    fireEvent.wheel(menu, { deltaY: 120 })
    expect(outerWheel).not.toHaveBeenCalled()

    fireEvent.pointerDown(input)
    expect(menu).toBeVisible()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('listbox', { name: '命令和技能建议' })).not.toBeInTheDocument()
    expect(input).toHaveValue('')
    await waitFor(() => expect(input).toHaveFocus())
    expect(input.selectionStart).toBe(0)
  })

  it('allows focus to move to an interactive target when outside pointer dismisses suggestions', () => {
    function ComposerHarness() {
      const [value, setValue] = useState('/')
      return (
        <>
          <Composer
            {...composerChromeProps()}
            value={value}
            isRunning={false}
            onChange={setValue}
            onSend={vi.fn()}
            onStop={vi.fn()}
          />
          <button type="button">外部操作</button>
        </>
      )
    }
    render(<ComposerHarness />)
    const target = screen.getByRole('button', { name: '外部操作' })

    fireEvent.mouseDown(target)
    target.focus()

    expect(screen.queryByRole('listbox', { name: '命令和技能建议' })).not.toBeInTheDocument()
    expect(target).toHaveFocus()
  })
})

it('引用附件后保留草稿并聚焦输入框', async () => {
  const props = { ...composerChromeProps(), value: '继续说明图中的内容', isRunning: false, onChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn() }
  const { rerender } = render(<Composer {...props} />)
  rerender(<Composer {...props} attachments={[{ id: 'reference', name: '报告.pdf', size: 12, kind: 'document', state: 'ready', progress: 100, reference: true }]} />)
  await waitFor(() => expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveFocus())
  expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('继续说明图中的内容')
  expect(props.onSend).not.toHaveBeenCalled()
})


it('卡片关闭结算后显示输入框并恢复焦点', async () => {
  const props = { ...composerChromeProps(), value: '继续讨论', isRunning: false,
    onChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn() }
  const { rerender } = render(<Composer {...props} takeover={<section aria-label="计划卡片">等待审阅</section>} />)
  expect(screen.getByRole('region', { name: '计划卡片' })).toBeVisible()
  expect(screen.queryByRole('textbox', { name: '消息输入' })).not.toBeInTheDocument()
  rerender(<Composer {...props} isRunning />)
  rerender(<Composer {...props} />)
  await waitFor(() => expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveFocus())
  expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('继续讨论')
  expect(props.onSend).not.toHaveBeenCalled()
})

it.each(['click', 'Enter', 'Tab'])('命令内光标选中 compact 立即执行：%s；移除完整命令并保留附件及后方草稿', (gesture) => {
  const onCompact = vi.fn(() => true)
  const onSend = vi.fn()
  const attachment: DraftAttachment = { id: 'draft-file', name: '草稿.pdf', kind: 'document', size: 3, state: 'uploading', progress: 20 }
  function Harness() {
    const [value, setValue] = useState(' 后续草稿')
    return <Composer {...composerChromeProps()} value={value} onChange={setValue} isRunning={false} onSend={onSend} onStop={vi.fn()} onCompact={onCompact} attachments={[attachment]} />
  }
  render(<Harness />)
  const input = screen.getByRole('textbox', { name: '消息输入' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '/compact 后续草稿', selectionStart: 5 } })
  expect(screen.getByRole('option', { name: /compact/ })).toBeEnabled()
  if (gesture === 'click') fireEvent.click(screen.getByRole('option', { name: /compact/ }))
  else fireEvent.keyDown(input, { key: gesture })
  expect(onCompact).toHaveBeenCalledOnce()
  expect(onSend).not.toHaveBeenCalled()
  expect(input).toHaveValue(' 后续草稿')
  expect(screen.getByRole('group', { name: '草稿.pdf' })).toBeVisible()
  expect(input).toHaveFocus()
})

it('从加号执行 compact 保留整个草稿，执行期间不可重复触发且仍能编辑', () => {
  const onCompact = vi.fn(() => true)
  const props = { ...composerChromeProps(), value: '后续草稿', onChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(), onCompact }
  const { rerender } = render(<Composer {...props} isRunning={false} />)
  fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
  fireEvent.click(screen.getByRole('option', { name: /compact/ }))
  expect(onCompact).toHaveBeenCalledOnce()
  expect(props.onChange).not.toHaveBeenCalled()
  rerender(<Composer {...props} isRunning />)
  fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
  expect(screen.getByRole('option', { name: /compact/ })).toBeDisabled()
  expect(screen.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
})
