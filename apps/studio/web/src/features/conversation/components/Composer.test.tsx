import { fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { useEffect, useRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { DraftAttachment } from '../useAttachments'
import { useComposerDraft } from '../useComposerDraft'
import { Composer as ComposerView } from './Composer'

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

  it('审批确认接管输入区时仍可停止当前运行', () => {
    const onStop = vi.fn()
    const view = render(<DraftComposer {...composerChromeProps()} text="" isRunning
      onDraftChange={vi.fn()} onSend={vi.fn()} onStop={onStop}
      submission={{ failed: false, checking: false, retry: vi.fn() }}
      takeover={<section aria-label="审批确认">保留原决定</section>} />)
    fireEvent.click(screen.getByRole('button', { name: '停止任务' }))
    expect(onStop).toHaveBeenCalledOnce()
    expect(screen.getByText('保留原决定')).not.toBeVisible()
    fireEvent.click(screen.getByText('查看提交内容'))
    expect(screen.getByRole('region', { name: '审批确认' })).toBeVisible()
    expect(screen.queryByRole('button', { name: '发送消息' })).not.toBeInTheDocument()
    view.rerender(<DraftComposer {...composerChromeProps()} text="" isRunning stopPending
      onDraftChange={vi.fn()} onSend={vi.fn()} onStop={onStop}
      submission={{ failed: false, checking: false, retry: vi.fn() }}
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

  it.each(['Enter', 'Tab'])('命令候选展开时 Shift+%s 保留原生编辑或焦点行为', key => {
    const onChooseModel = vi.fn()
    const onSend = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="/model" isRunning={false}
      onDraftChange={vi.fn()} onChooseModel={onChooseModel} onSend={onSend} onStop={vi.fn()} />)
    expect(screen.getByRole('listbox', { name: '命令和技能建议' })).toBeVisible()
    const input = screen.getByRole('textbox', { name: '消息输入' })
    expect(fireEvent.keyDown(input, { key, shiftKey: true })).toBe(true)
    expect(onChooseModel).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
    expect(input).toHaveValue('/model')
  })

  it.each(['Enter', 'Tab'])('命令候选展开时普通 %s 仍确认选择', key => {
    const onChooseModel = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="/model" isRunning={false}
      onDraftChange={vi.fn()} onChooseModel={onChooseModel} onSend={vi.fn()} onStop={vi.fn()} />)
    expect(fireEvent.keyDown(screen.getByRole('textbox', { name: '消息输入' }), { key })).toBe(false)
    expect(onChooseModel).toHaveBeenCalledOnce()
  })

  it('提交暂不可用时保留草稿编辑与模型命令，恢复后可发送', () => {
    const onSend = vi.fn()
    const onChooseModel = vi.fn()
    const props = { ...composerChromeProps(), text: '保留草稿', isRunning: false,
      onDraftChange: vi.fn(), onSend, onChooseModel, onStop: vi.fn() }
    const view = render(<DraftComposer {...props} submissionDisabledReason="请选择可用模型" />)
    const input = screen.getByRole('textbox', { name: '消息输入' })
    expect(input).toBeEnabled()
    expect(screen.getByRole('status')).toHaveTextContent('请选择可用模型')
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: '/model', selectionStart: 6 } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChooseModel).toHaveBeenCalledOnce()
    fireEvent.change(input, { target: { value: '继续编辑', selectionStart: 4 } })
    expect(input).toHaveValue('继续编辑')
    view.rerender(<DraftComposer {...props} />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(onSend).toHaveBeenCalledOnce()
  })

  it('模型不可用于新提交时仍能停止当前运行', () => {
    const onStop = vi.fn()
    render(<DraftComposer {...composerChromeProps()} text="保留草稿" isRunning
      submissionDisabledReason="请选择可用模型" onDraftChange={vi.fn()} onSend={vi.fn()} onStop={onStop} />)
    fireEvent.click(screen.getByRole('button', { name: '停止任务' }))
    expect(onStop).toHaveBeenCalledOnce()
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
})
