import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, useRef, useState } from 'react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { Composer as ComposerView } from './Composer'
import { useComposerDraft } from '../useComposerDraft'
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

function DraftComposer({ text, onDraftChange, ...props }: Omit<ComponentProps<typeof ComposerView>, 'draft' | 'onChange'> & {
  text: string
  onDraftChange: (text: string) => void
}) {
  const draft = useComposerDraft(text)
  const latest = useRef(draft.state)
  latest.current = draft.state
  const { setText } = draft
  useEffect(() => {
    if (latest.current.doc.toString() !== text) setText(text)
  }, [text, setText])
  return <ComposerView {...props} draft={draft.state}
    selectedSkills={props.selectedSkills ?? draft.references.map(reference => reference.skill)}
    onChange={transaction => { draft.apply(transaction); if (transaction.docChanged) onDraftChange(transaction.newDoc.toString()) }} />
}

describe('Composer', () => {
  it.each([
    { label: '无技能', skills: [], skillsStatus: 'ready' as const },
    { label: '技能描述匹配部分路径', skills: [{ id: 'find-skills', name: 'find-skills', description: 'Discover and install skills' }], skillsStatus: 'ready' as const },
    { label: '技能目录读取中', skills: [], skillsStatus: 'loading' as const },
    { label: '技能目录读取失败', skills: [], skillsStatus: 'error' as const },
  ])('$label 时逐字符输入保留完整路径，不自动绑定技能', ({ skills, skillsStatus }) => {
    render(<DraftComposer {...composerChromeProps()} text="" onDraftChange={vi.fn()} isRunning={false}
      onSend={vi.fn()} onStop={vi.fn()} skills={skills} skillsStatus={skillsStatus} />)
    const input = screen.getByRole('textbox', { name: '消息输入' }) as HTMLTextAreaElement
    for (const character of '/scripts') {
      const next = input.value + character
      fireEvent.change(input, { target: { value: next, selectionStart: next.length } })
    }
    expect(input).toHaveValue('/scripts')
    expect(input).not.toHaveAccessibleDescription()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('路径粘贴、撤销与重做保留原文和发送能力', async () => {
    const interaction = userEvent.setup()
    render(<DraftComposer {...composerChromeProps()} text="" onDraftChange={vi.fn()} isRunning={false}
      onSend={vi.fn()} onStop={vi.fn()} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    await interaction.click(input)
    await interaction.paste('/scripts/run.py')
    expect(input).toHaveValue('/scripts/run.py')
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
    await interaction.keyboard('{Control>}z{/Control}')
    expect(input).toHaveValue('')
    await interaction.keyboard('{Control>}{Shift>}z{/Shift}{/Control}')
    expect(input).toHaveValue('/scripts/run.py')
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('已保存的路径草稿可通过发送按钮或Enter发送', () => {
    const onSend = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="/scripts" onDraftChange={vi.fn()} isRunning={false}
      onSend={onSend} onStop={vi.fn()} />)
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeEnabled()
    fireEvent.click(send)
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息输入' }), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(2)
  })

  it('路径中的中文组合输入保留后续编辑，确认候选时不发送', () => {
    const onSend = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="/scripts/" onDraftChange={vi.fn()} isRunning={false}
      onSend={onSend} onStop={vi.fn()} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    fireEvent.compositionStart(input)
    fireEvent.change(input, { target: { value: '/scripts/脚', selectionStart: 10 } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.compositionEnd(input)
    fireEvent.change(input, { target: { value: '/scripts/脚本', selectionStart: 11 } })
    expect(input).toHaveValue('/scripts/脚本')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledOnce()
  })

  it('选择技能在光标处插入主题引用，删除和撤销同步所选身份', () => {
    const skills = [{ id: 'one', name: 'reports', description: 'Prepare reports' }, { id: 'two', name: 'research', description: 'Research sources' }]
    render(<DraftComposer {...composerChromeProps()} text="保留正文" onDraftChange={vi.fn()} isRunning={false} onSend={vi.fn()} onStop={vi.fn()} skills={skills} />)
    const input = screen.getByRole('textbox', { name: '消息输入' }) as HTMLTextAreaElement
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    expect(screen.getByText('技能（2）')).toBeVisible()
    fireEvent.click(screen.getByRole('option', { name: /reports Prepare reports/ }))
    expect(input).toHaveValue('保留正文/reports')
    fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
    expect(screen.getByRole('option', { name: /reports Prepare reports/ })).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Escape' })
    fireEvent.keyDown(input, { key: 'Backspace' })
    expect(input).toHaveValue('保留正文')
    fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
    expect(input).toHaveValue('保留正文/reports')
  })

  it('输入技能前缀后用 Enter 选择，并保留用户原文', () => {
    render(<DraftComposer {...composerChromeProps()} text="" onDraftChange={vi.fn()} isRunning={false} onSend={vi.fn()} onStop={vi.fn()}
      skills={[{ id: 'one', name: 'reports', description: 'Prepare reports' }]} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    fireEvent.change(input, { target: { value: '/repo', selectionStart: 5 } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('用 /reports 技能帮我')
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  })

  it('技能不可用时保留草稿并阻止发送', () => {
    const onSend = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="保留正文 /reports" onDraftChange={vi.fn()} isRunning={false} onSend={onSend} onStop={vi.fn()}
      selectedSkills={[{ id: 'reports', name: 'reports', description: '整理报告', unavailable: true }]} />)
    expect(screen.getByRole('status')).toHaveTextContent('所选技能已停用或卸载，请移除后再发送')
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息输入' }), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })
  it('图片上传就绪后允许发送并保留正文与附件', () => {
    const attachment: DraftAttachment = {
      id: 'image', name: '截图.png', kind: 'image', size: 3, state: 'ready', progress: 100,
    }
    const onSend = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="保留正文" isRunning={false} attachments={[attachment]}
      onDraftChange={vi.fn()} onSend={onSend} onStop={vi.fn()} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(input).not.toHaveAccessibleDescription()
    expect(send).not.toHaveAccessibleDescription()
    expect(send).toBeEnabled()
    expect(screen.getByRole('group', { name: '截图.png' })).toBeVisible()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledOnce()
    expect(input).toHaveValue('保留正文')
  })

  it.each(['loading', 'error'] as const)('技能列表为 %s 时保留标签和草稿，阻止普通发送直到读取完成', skillsStatus => {
    const onSend = vi.fn()
    const props = { ...composerChromeProps(), text: '保留正文', onDraftChange: vi.fn(), isRunning: false, onSend, onStop: vi.fn(),
      selectedSkills: [{ id: 'reports', name: 'reports', description: '整理报告' }] }
    const { rerender } = render(<DraftComposer {...props} skillsStatus={skillsStatus} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(screen.getByRole('status')).toHaveTextContent(skillsStatus === 'loading' ? '正在加载技能' : '技能列表暂不可用')
    expect(send).toBeDisabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    expect(input).toHaveValue('保留正文')

    rerender(<DraftComposer {...props} skillsStatus="ready" />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(send).toBeEnabled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledOnce()
  })

  it('技能列表加载不阻断无选择的普通发送或沿用原运行的压缩', () => {
    const onSend = vi.fn()
    const props = { ...composerChromeProps(), text: '普通正文', onDraftChange: vi.fn(), isRunning: false, onSend, onStop: vi.fn() }
    const { rerender } = render(<DraftComposer {...props} skillsStatus="loading" />)
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(onSend).toHaveBeenCalledOnce()

    rerender(<DraftComposer {...props} text="/compact " skillsStatus="loading" selectedSkills={[{ id: 'reports', name: 'reports', description: '整理报告' }]} />)
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(onSend).toHaveBeenCalledTimes(2)
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
      ...composerChromeProps(), text: '保留正文', isRunning: false,
      onDraftChange: vi.fn(), onSend, onStop: vi.fn(),
    }
    const { rerender } = render(<DraftComposer {...props} attachments={[ready, pending]} />)
    const send = screen.getByRole('button', { name: '发送消息' })
    const input = screen.getByRole('textbox', { name: '消息输入' })
    expect(send).toBeDisabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    expect(input).toHaveValue('保留正文')
    rerender(<DraftComposer {...props} attachments={[ready, { ...pending, state: 'ready', attachment: ready.attachment }]} />)
    expect(send).toBeEnabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(2)
  })

  it('失败卡片显示明确状态，支持重试和移除，移除后可发送正文', () => {
    const error = '网络请求失败，请稍后重试'
    const failed: DraftAttachment = {
      id: 'failed', name: '报告.pdf', kind: 'document', size: 3,
      state: 'error', progress: 0, error,
    }
    const onRetryAttachment = vi.fn()
    const onRemoveAttachment = vi.fn()
    const props = {
      ...composerChromeProps(), text: '保留正文', isRunning: false,
      onDraftChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(), onRetryAttachment, onRemoveAttachment,
    }
    const { rerender } = render(<DraftComposer {...props} attachments={[failed]} />)
    const card = screen.getByRole('group', { name: '报告.pdf' })
    expect(within(card).getByText('PDF')).toBeInTheDocument()
    expect(within(card).getByRole('status')).toHaveTextContent('上传失败')
    expect(within(card).getByRole('status')).toHaveAccessibleDescription(error)
    fireEvent.pointerMove(within(card).getByRole('status'))
    expect(screen.getByRole('tooltip')).toHaveTextContent(error)
    fireEvent.click(within(card).getByRole('button', { name: '重试附件：报告.pdf' }))
    expect(onRetryAttachment).toHaveBeenCalledWith('failed')
    rerender(<DraftComposer {...props} attachments={[{ ...failed, state: 'uploading', progress: 42 }]} />)
    expect(card).toHaveAttribute('aria-busy', 'true')
    expect(within(card).getByRole('status')).toHaveTextContent('上传中 42%')
    expect(within(card).queryByText('上传失败')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    rerender(<DraftComposer {...props} attachments={[failed]} />)
    fireEvent.click(within(card).getByRole('button', { name: '移除附件：报告.pdf' }))
    expect(onRemoveAttachment).toHaveBeenCalledWith('failed')
    rerender(<DraftComposer {...props} attachments={[]} />)
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
    expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('保留正文')
  })

  it('按传入控件显示返回底部和轨迹入口，移除后不再提供入口', () => {
    const props = {
      ...composerChromeProps(), text: '', isRunning: false,
      onDraftChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(),
    }
    const { rerender } = render(
      <DraftComposer
        {...props}
        scrollToBottomControl={<button type="button">回到底部</button>}
        taskTraceControl={<button type="button">任务轨迹 2</button>}
      />,
    )
    expect(screen.getByRole('button', { name: '回到底部' })).toBeVisible()
    expect(screen.getByRole('button', { name: '任务轨迹 2' })).toBeVisible()
    rerender(<DraftComposer {...props} />)
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '任务轨迹 2' })).not.toBeInTheDocument()
  })

  it('接管时隐藏输入控件，结束后恢复草稿和焦点', () => {
    const animation = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    const { rerender } = render(
      <DraftComposer
        {...composerChromeProps()}
        takeover={<section aria-label="澄清接管">等待回答</section>}
        text="保留的草稿"
        isRunning={false}
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        text="保留的草稿"
        isRunning={false}
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        text=""
        isRunning={false}
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        modelControl={<button type="button">GPT-5.5</button>}
        accessControl={<button type="button">选择访问权限</button>}
        planActive
        onExitPlan={onExitPlan}
        text=""
        isRunning={false}
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        attachments={[attachment]}
        onAddAttachments={onAddAttachments}
        onRemoveAttachment={onRemoveAttachment}
        text="正文"
        isRunning={false}
        onDraftChange={vi.fn()}
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
          <DraftComposer
            {...composerChromeProps()}
            text={value}
            isRunning={false}
            onDraftChange={setValue}
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
          <DraftComposer
            {...composerChromeProps()}
            text={value}
            isRunning={false}
            onDraftChange={setValue}
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

  it('普通斜杠文本不被Escape删除，候选取消只移除光标前的触发词', () => {
    function ComposerHarness() {
      const [value, setValue] = useState('已有内容')
      return (
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
    expect(input).toHaveValue('/x已有内容')
    expect(input.selectionStart).toBe(2)
    expect(screen.queryByRole('listbox', { name: '命令和技能建议' })).not.toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('/x已有内容')

    fireEvent.change(input, { target: { value: '/已有内容', selectionStart: 1 } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('已有内容')
    expect(input.selectionStart).toBe(0)
  })

  it('keeps wheel input inside the composer even when the input does not overflow', () => {
    const outerWheel = vi.fn()
    render(<div onWheel={outerWheel}>
      <DraftComposer {...composerChromeProps()} text="" isRunning={false} onDraftChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />
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
      <DraftComposer
        {...composerChromeProps()}
        text="正在发送"
        isRunning
        onDraftChange={vi.fn()}
        onSend={vi.fn()}
        onStop={onStop}
      />,
    )

    const stopButton = screen.getByRole('button', { name: '停止任务' })
    fireEvent.click(stopButton)
    expect(onStop).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: '发送消息' })).not.toBeInTheDocument()
  })

  it('审批确认接管输入区时仍可停止当前运行', () => {
    const onStop = vi.fn()
    const view = render(<DraftComposer {...composerChromeProps()} text="" isRunning
      onDraftChange={vi.fn()} onSend={vi.fn()} onStop={onStop}
      takeover={<section aria-label="审批确认">保留原决定</section>} />)
    fireEvent.click(screen.getByRole('button', { name: '停止任务' }))
    expect(onStop).toHaveBeenCalledOnce()
    expect(screen.getByRole('region', { name: '审批确认' })).toBeVisible()
    expect(screen.queryByRole('button', { name: '发送消息' })).not.toBeInTheDocument()
    view.rerender(<DraftComposer {...composerChromeProps()} text="" isRunning stopPending
      onDraftChange={vi.fn()} onSend={vi.fn()} onStop={onStop}
      takeover={<section aria-label="审批确认">保留原决定</section>} />)
    expect(screen.getByRole('button', { name: '正在停止任务' })).toBeDisabled()
  })

  it.each([
    ['composition state', { isComposing: true }],
    ['IME compatibility key code', { keyCode: 229 }],
  ])('does not send while Enter confirms an IME %s', (_name, nativeFields) => {
    const onSend = vi.fn()
    render(
      <DraftComposer
        {...composerChromeProps()}
        text="拼音输入"
        isRunning={false}
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        text="可发送内容"
        isRunning={false}
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        text="暂存内容"
        isRunning={false}
        isHydrating
        onDraftChange={vi.fn()}
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
      <DraftComposer
        {...composerChromeProps()}
        text="已有内容"
        isRunning={false}
        onDraftChange={vi.fn()}
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
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
      <DraftComposer
        {...composerChromeProps()}
        text="/"
        isRunning={false}
        onDraftChange={onChange}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    const menu = screen.getByRole('listbox', { name: '命令和技能建议' })
    const commandGroup = within(menu).getByRole('group', { name: '指令' })
    const skillGroup = within(menu).getByRole('group', { name: '技能' })
    expect(within(commandGroup).getByText('指令（4）')).toBeVisible()
    expect(within(skillGroup).getByText('技能（0）')).toBeVisible()
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
    render(<DraftComposer {...composerChromeProps()} text="保留正文" isRunning={false}
      onDraftChange={onChange} onSend={onSend} onStop={vi.fn()} onChooseModel={onChooseModel} />)
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
      return <DraftComposer {...composerChromeProps()} text={value} isRunning={false}
        onDraftChange={setValue} onSend={onSend} onStop={vi.fn()} onChooseModel={onChooseModel} />
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

  it('普通斜杠文本可编辑和发送，已知命令前缀仍等待完成', () => {
    function ComposerHarness() {
      const [value, setValue] = useState('')
      return (
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
    expect(input).toHaveValue('/px')
    expect(send).toBeEnabled()
    fireEvent.change(input, { target: { value: '/export', selectionStart: 7 } })
    expect(input).toHaveValue('/export')
    expect(send).toBeEnabled()

    fireEvent.change(input, { target: { value: '/plan', selectionStart: 5 } })
    expect(input).toHaveValue('/plan')
    expect(send).toBeDisabled()
  })

  it.each(['/plan', '/plan ', '  /plan \n\t'])('Plan 任务正文为空时禁用发送并阻止 Enter：%j', (value) => {
    const onSend = vi.fn()
    const props = {
      ...composerChromeProps(), text: value, isRunning: false,
      onDraftChange: vi.fn(), onSend, onStop: vi.fn(),
    }
    const { rerender } = render(<DraftComposer {...props} />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeDisabled()
    fireEvent.click(send)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<DraftComposer {...props} text="/plan 制定方案" />)
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
      onDraftChange: vi.fn(), onSend, onStop: vi.fn(),
    }
    const { rerender } = render(<DraftComposer {...props} text="/plan " />)
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeDisabled()
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息输入' }), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<DraftComposer {...props} text="" />)
    expect(send).toBeEnabled()
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledOnce()
  })

  it.each(['/plan ', '/plan'])('removes the complete %s command with one Backspace', (initialValue) => {
    function ComposerHarness() {
      const [value, setValue] = useState(initialValue)
      return (
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
        <DraftComposer
          {...composerChromeProps()}
          text={value}
          isRunning={false}
          onDraftChange={setValue}
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
          <DraftComposer
            {...composerChromeProps()}
            text={value}
            isRunning={false}
            onDraftChange={setValue}
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
  const props = { ...composerChromeProps(), text: '继续说明图中的内容', isRunning: false, onDraftChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn() }
  const { rerender } = render(<DraftComposer {...props} />)
  rerender(<DraftComposer {...props} attachments={[{ id: 'reference', name: '报告.pdf', size: 12, kind: 'document', state: 'ready', progress: 100, reference: true }]} />)
  await waitFor(() => expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveFocus())
  expect(screen.getByRole('textbox', { name: '消息输入' })).toHaveValue('继续说明图中的内容')
  expect(props.onSend).not.toHaveBeenCalled()
})


it('卡片关闭结算后显示输入框并恢复焦点', async () => {
  const props = { ...composerChromeProps(), text: '继续讨论', isRunning: false,
    onDraftChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn() }
  const { rerender } = render(<DraftComposer {...props} takeover={<section aria-label="计划卡片">等待审阅</section>} />)
  expect(screen.getByRole('region', { name: '计划卡片' })).toBeVisible()
  expect(screen.queryByRole('textbox', { name: '消息输入' })).not.toBeInTheDocument()
  rerender(<DraftComposer {...props} isRunning />)
  rerender(<DraftComposer {...props} />)
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
    return <DraftComposer {...composerChromeProps()} text={value} onDraftChange={setValue} isRunning={false} onSend={onSend} onStop={vi.fn()} onCompact={onCompact} attachments={[attachment]} />
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
  const props = { ...composerChromeProps(), text: '后续草稿', onDraftChange: vi.fn(), onSend: vi.fn(), onStop: vi.fn(), onCompact }
  const { rerender } = render(<DraftComposer {...props} isRunning={false} />)
  fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
  fireEvent.click(screen.getByRole('option', { name: /compact/ }))
  expect(onCompact).toHaveBeenCalledOnce()
  expect(props.onDraftChange).not.toHaveBeenCalled()
  rerender(<DraftComposer {...props} isRunning />)
  fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
  expect(screen.getByRole('option', { name: /compact/ })).toBeDisabled()
  expect(screen.getByRole('textbox', { name: '消息输入' })).toBeEnabled()
})


it('删除技能前后的中文后仍可发送，引用本体删除和撤销与草稿一致', () => {
  const onSend = vi.fn()
  render(<DraftComposer {...composerChromeProps()} text="" onDraftChange={vi.fn()} isRunning={false} onSend={onSend} onStop={vi.fn()}
    skills={[{ id: 'report', name: 'ai-report-interpreter', description: '解读报告' }]} />)
  fireEvent.click(screen.getByRole('button', { name: '打开命令和技能' }))
  fireEvent.click(screen.getByRole('option', { name: /ai-report-interpreter 解读报告/ }))
  const input = screen.getByRole('textbox', { name: '消息输入' }) as HTMLTextAreaElement
  expect(input).toHaveAccessibleDescription('技能引用可整段删除，撤销可恢复')
  input.setSelectionRange(24, input.value.length)
  fireEvent.select(input)
  fireEvent.change(input, { target: { value: '用 /ai-report-interpreter', selectionStart: 24 } })
  expect(input).toHaveAccessibleDescription('技能引用可整段删除，撤销可恢复')
  input.setSelectionRange(1, 2)
  fireEvent.select(input)
  fireEvent.change(input, { target: { value: '用/ai-report-interpreter', selectionStart: 1 } })
  expect(input).toHaveValue('用/ai-report-interpreter')
  expect(input).toHaveAccessibleDescription('技能引用可整段删除，撤销可恢复')
  input.setSelectionRange(0, 1)
  fireEvent.select(input)
  fireEvent.change(input, { target: { value: '/ai-report-interpreter', selectionStart: 0 } })
  expect(input).toHaveValue('/ai-report-interpreter')
  expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled()
  fireEvent.keyDown(input, { key: 'Delete' })
  expect(input).toHaveValue('')
  fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
  expect(input).toHaveValue('/ai-report-interpreter')
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  expect(onSend).toHaveBeenCalledOnce()
})
