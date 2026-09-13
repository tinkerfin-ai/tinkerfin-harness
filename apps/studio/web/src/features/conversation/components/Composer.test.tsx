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
  onAddAttachments: vi.fn(),
  onRemoveAttachment: vi.fn(),
})

describe('Composer', () => {
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

  it('保留普通会话的辅助栏空间，并排列返回底部与任务轨迹操作', () => {
    const { container, rerender } = render(
      <Composer
        {...composerChromeProps()}
        scrollToBottomControl={<button type="button">回到底部</button>}
        taskTraceControl={(
          <>
            <button type="button">链路</button>
            <button type="button">任务轨迹 2</button>
          </>
        )}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const controls = container.querySelector('.composer-auxiliary-controls')
    expect(controls).not.toBeNull()
    expect(within(controls as HTMLElement).getAllByRole('button').map((button) => button.textContent))
      .toEqual(['回到底部', '链路', '任务轨迹 2'])
    expect(screen.getByRole('button', { name: '回到底部' }).parentElement)
      .toHaveClass('composer-scroll-to-bottom-control')
    expect(screen.getByRole('button', { name: '任务轨迹 2' }).parentElement)
      .toHaveClass('composer-task-trace-control')
    expect(screen.getByRole('button', { name: '链路' }).parentElement)
      .toBe(screen.getByRole('button', { name: '任务轨迹 2' }).parentElement)

    rerender(
      <Composer
        {...composerChromeProps()}
        taskTraceControl={<button type="button">任务轨迹 2</button>}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '任务轨迹 2' }).parentElement)
      .toHaveClass('composer-task-trace-control')

    rerender(
      <Composer
        {...composerChromeProps()}
        scrollToBottomControl={<button type="button">回到底部</button>}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: '回到底部' }).parentElement)
      .toHaveClass('composer-scroll-to-bottom-control')
    expect(screen.queryByRole('button', { name: '任务轨迹 2' })).not.toBeInTheDocument()

    rerender(
      <Composer
        {...composerChromeProps()}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    expect(container.querySelector('.composer-auxiliary-controls')).toBeEmptyDOMElement()
  })

  it('keeps the default composer mounted and inert during a takeover, then restores focus', () => {
    const animation = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    const { container, rerender } = render(
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

    const fallback = container.querySelector('.composer-default')
    const input = screen.getByLabelText('消息输入')
    const note = screen.getByText('TinkerFin 可能会犯错，请核对重要信息')
    expect(fallback).toHaveClass('is-taken-over')
    expect(fallback).toHaveAttribute('inert')
    expect(input).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '澄清接管' })).toBeVisible()
    expect(container.querySelector('.composer-auxiliary-controls')).toBeEmptyDOMElement()
    expect(note).toBeVisible()
    expect(fallback).not.toContainElement(note)
    expect(container.querySelector('.composer-takeover')).not.toContainElement(note)

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
    expect(fallback).not.toHaveClass('is-taken-over')
    expect(input).toHaveFocus()
    expect(input).toHaveValue('保留的草稿')
    expect(screen.getByText('TinkerFin 可能会犯错，请核对重要信息')).toBe(note)
    animation.mockRestore()
  })

  it('uses the branded placeholder and polished send icon', () => {
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
    expect(sendButton.querySelector('.lucide-arrow-up')).not.toBeNull()
    expect(sendButton.closest('.composer')).toHaveClass('composer')
    expect(screen.queryByText(/Shift \+ Enter/)).not.toBeInTheDocument()
  })

  it('places add and Plan on the left with model and send on the right', () => {
    const onExitPlan = vi.fn()
    const { container } = render(
      <Composer
        {...composerChromeProps()}
        modelControl={<button type="button">GPT-5.5</button>}
        planActive
        onExitPlan={onExitPlan}
        value=""
        isRunning={false}
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const toolbar = container.querySelector('.composer-toolbar')
    if (!(toolbar instanceof HTMLElement)) throw new Error('missing composer toolbar')
    expect(within(toolbar).getAllByRole('button').map((button) => button.getAttribute('aria-label') ?? button.textContent)).toEqual([
      '添加本地附件',
      'Plan 已开启，点击关闭',
      'GPT-5.5',
      '发送消息',
    ])
    const planChip = screen.getByRole('button', { name: 'Plan 已开启，点击关闭' })
    expect(planChip).not.toHaveClass('ui-button')
    expect(planChip).toHaveAttribute('title', 'Plan 已开启 — 点击关闭')
    expect(planChip.querySelector('.composer-plan-chip-close svg')).toHaveAttribute('width', '12')
    expect(planChip.querySelector('.composer-plan-chip-close svg')).toHaveAttribute('height', '12')
    fireEvent.click(planChip)
    expect(onExitPlan).toHaveBeenCalledTimes(1)
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
        attachmentError="附件总大小不能超过 25MB"
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
    const attachmentRow = container.querySelector('.composer-attachments')
    const inputScroll = container.querySelector('.composer-input-scroll')
    expect(attachmentRow).not.toBeNull()
    expect(inputScroll).not.toBeNull()
    expect(attachmentRow?.compareDocumentPosition(inputScroll as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(container.querySelector('.composer-attachment')).toHaveTextContent('brief.pdf')
    expect(screen.getByText('附件总大小不能超过 25MB')).toHaveAttribute('aria-live', 'polite')
    fireEvent.click(screen.getByRole('button', { name: '移除附件：brief.pdf' }))
    expect(onRemoveAttachment).toHaveBeenCalledWith('attachment-1')

    const fileInput = container.querySelector('input[type="file"]')
    if (!(fileInput instanceof HTMLInputElement)) throw new Error('missing attachment input')
    const image = new File(['image'], 'chart.png', { type: 'image/png' })
    fireEvent.change(fileInput, { target: { files: [image] } })
    expect(onAddAttachments).toHaveBeenCalledWith([image])
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('focuses the input from the full composer card hit area', () => {
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

    const input = screen.getByLabelText('消息输入')
    fireEvent.pointerDown(input.closest('.composer') as HTMLElement)
    expect(input).toHaveFocus()
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
    const inputScroll = input.closest('.composer-input-scroll')!
    Object.defineProperty(inputScroll, 'scrollHeight', { configurable: true, value: 220 })
    Object.defineProperty(inputScroll, 'clientHeight', { configurable: true, value: 144 })
    fireEvent.wheel(input, { deltaY: 120 })
    expect(outerWheel).not.toHaveBeenCalled()
  })

  it('shows the dedicated running stop control', () => {
    render(
      <Composer
        {...composerChromeProps()}
        value="正在发送"
        isRunning
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const stopButton = screen.getByRole('button', { name: '停止任务' })
    expect(stopButton).toHaveClass('stop')
    expect(stopButton.querySelector('.lucide-square')).not.toBeNull()
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
  })

  it('shows the DSH command and skill inventory with Plan as the only enabled item', () => {
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
    const commandGroup = within(menu).getByRole('group', { name: '命令' })
    const skillGroup = within(menu).getByRole('group', { name: '技能' })
    expect(within(commandGroup).getAllByRole('option')).toHaveLength(7)
    expect(within(skillGroup).getAllByRole('option')).toHaveLength(1)

    const plan = within(commandGroup).getByRole('option', { name: /plan 进入 Plan 模式/ })
    const disabledOptions = within(menu).getAllByRole('option').filter((option) => option !== plan)
    expect(plan).toBeEnabled()
    disabledOptions.forEach((option) => expect(option).toBeDisabled())

    fireEvent.mouseDown(within(commandGroup).getByRole('option', { name: /compact/ }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.mouseDown(plan)
    expect(onChange).toHaveBeenCalledWith('/plan ')
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
    fireEvent.change(input, { target: { value: '/compact', selectionStart: 8 } })
    expect(input).toHaveValue('/p')

    fireEvent.change(input, { target: { value: '/plan', selectionStart: 5 } })
    expect(input).toHaveValue('/plan')
    expect(send).toBeEnabled()
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
    const { container } = render(<ComposerHarness />)
    const input = screen.getByLabelText('消息输入') as HTMLTextAreaElement
    input.focus()
    fireEvent.change(input, { target: { value: '/', selectionStart: 1 } })

    expect(screen.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()
    expect(input).toHaveAttribute('aria-activedescendant', expect.stringContaining('command-plan'))
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key })

    expect(input).toHaveValue('/plan ')
    expect(input.selectionStart).toBe(6)
    expect(input).toHaveFocus()
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox', { name: '命令和技能建议' })).not.toBeInTheDocument()
    const backdrop = container.querySelector('.composer-input-backdrop')
    expect(backdrop?.querySelector('mark')?.textContent).toBe('/plan ')
    expect(backdrop).toHaveTextContent('描述你的任务以生成计划')
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
