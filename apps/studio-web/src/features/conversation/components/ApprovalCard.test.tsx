import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { buildEmptyConversation } from '../../../lib/workspace'
import type { ApprovalItem, ApprovalState, Conversation } from '../../../types'
import { ApprovalCard } from './ApprovalCard'

const approvalItem = (
  id: string,
  filePath: string,
  options: Partial<ApprovalItem> = {},
): ApprovalItem => ({
  id,
  interruptId: `interrupt-${id}`,
  toolCallId: `tool-${id}`,
  toolName: 'write_file',
  params: JSON.stringify({ file_path: filePath, content: `content-${id}` }, null, 2),
  input: filePath,
  description: `写入 ${filePath}`,
  originalArgs: { file_path: filePath, content: `content-${id}` },
  allowedDecisions: ['approve', 'reject'],
  ...options,
})

const conversationWithApproval = (
  approval: ApprovalState,
  threadId = 'thread-approval',
): Conversation => ({
  ...buildEmptyConversation({projectId: 'project-1',
    threadId,
    now: '2026-08-25T00:00:00.000Z',
    model: 'GPT-5.5',
  }),
  runStatus: 'waiting_approval',
  approval,
})

describe('ApprovalCard', () => {
  beforeEach(() => window.sessionStorage.clear())

  it('提交尚未确认时显示原决定并禁止重复授权', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const onChange = vi.fn()
    const retry = vi.fn()
    const conversation = conversationWithApproval({
      activeIndex: 0, submitted: true, submissionRunId: 'resume-run', mode: 'reject',
      items: [approvalItem('first', '/first.txt', { decision: 'rejected', rejectionReason: '保留原文件' })],
    })
    const view = render(<ApprovalCard conversation={conversation} onChange={onChange} onSubmit={onSubmit} />)
    expect(screen.getByRole('region', { name: '提交确认' })).toBeVisible()
    expect(screen.getByText('正在提交')).toHaveAttribute('role', 'status')
    expect(screen.getByRole('textbox')).toHaveValue('保留原文件')
    expect(screen.getByRole('textbox')).toBeDisabled()
    for (const name of ['允许', '拒绝', '取消', '确认拒绝', '重新提交', '重新检查状态']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    view.rerender(<ApprovalCard conversation={conversation} onChange={onChange} onSubmit={onSubmit}
      submission={{ failed: true, checking: false, retry }} />)
    expect(screen.getByText('暂时无法确认提交结果')).toHaveAttribute('role', 'status')
    expect(screen.getByRole('textbox')).toHaveValue('保留原文件')
    await user.click(screen.getByRole('button', { name: '重新检查状态' }))
    expect(retry).toHaveBeenCalledOnce()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('显示完整列表，逐项决定后只提交一次并保留顺序', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    function Harness() {
      const [approval, setApproval] = useState<ApprovalState>({
        activeIndex: 0, submitted: false, mode: 'options',
        items: [approvalItem('first', '/first.txt'), approvalItem('second', '/second.txt')],
      })
      return <ApprovalCard conversation={conversationWithApproval(approval)} onChange={setApproval} onSubmit={onSubmit} />
    }
    render(<Harness />)
    expect(screen.getByRole('heading')).toHaveTextContent('2 项操作待审批')
    expect(screen.getByText('/first.txt')).toBeVisible()
    expect(screen.getByText('/second.txt')).toBeVisible()
    const first = within(screen.getByRole('group', { name: '审批项 1' }))
    const second = within(screen.getByRole('group', { name: '审批项 2' }))
    await user.click(first.getByRole('button', { name: '允许' }))
    expect(first.getByRole('button', { name: '允许' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(second.getByRole('button', { name: '拒绝' }))
    expect(onSubmit).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '提交决定' }))
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith(['interrupt-first', 'interrupt-second'])
  })

  it.each(['approved', 'rejected'] as const)('批量 %s 一次传递整组决定，尊重每项 allowedDecisions', async decision => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const approval: ApprovalState = {
      activeIndex: 0, submitted: false,
      items: Array.from({ length: 11 }, (_, index) => approvalItem(String(index), '/' + index + '.txt')),
    }
    const view = render(<ApprovalCard conversation={conversationWithApproval(approval)} onChange={vi.fn()} onSubmit={onSubmit} />)
    await user.click(screen.getByRole('button', { name: decision === 'approved' ? '全部允许' : '全部拒绝' }))
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith(
      approval.items.map(item => item.interruptId),
      approval.items.map(item => ({ interruptId: item.interruptId, decision })),
    )
    view.rerender(<ApprovalCard conversation={conversationWithApproval({
      ...approval, items: approval.items.map((item, index) => index === 0 ? { ...item, allowedDecisions: ['reject'] } : item),
    })} onChange={vi.fn()} onSubmit={onSubmit} />)
    expect(screen.queryByRole('button', { name: '全部允许' })).not.toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: '审批项 1' })).queryByRole('button', { name: '允许' })).not.toBeInTheDocument()
  })

  it('updates only approval state and preserves a concurrent conversation message', () => {
    const initial = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        approvalItem('first', '/first.txt'),
        approvalItem('second', '/second.txt'),
      ],
    })
    let authoritative = initial
    render(
      <ApprovalCard
        conversation={initial}
        onChange={(change) => {
          if (!authoritative.approval) return
          authoritative = {
            ...authoritative,
            approval: change(authoritative.approval),
          }
        }}
        onSubmit={vi.fn()}
      />,
    )
    authoritative = {
      ...authoritative,
      messages: [{
        id: 'message-concurrent',
        role: 'assistant',
        content: '审批期间到达的消息',
        createdAt: '2026-08-25T00:00:01.000Z',
      }],
    }

    fireEvent.click(within(screen.getByRole('group', { name: '审批项 1' })).getByRole('button', { name: '允许' }))

    expect(authoritative.messages.map((message) => message.id)).toEqual(['message-concurrent'])
    expect(authoritative.approval?.items[0]?.decision).toBe('approved')
    expect(authoritative.approval?.activeIndex).toBe(1)
  })

  it('does not apply an old card action after the authoritative approval group changes', () => {
    const initial = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        approvalItem('old', '/old.txt'),
        approvalItem('old-next', '/old-next.txt'),
      ],
    })
    let authoritativeApproval: ApprovalState = {
      activeIndex: 0,
      submitted: false,
      mode: 'options',
      items: [
        approvalItem('new', '/new.txt'),
        approvalItem('new-next', '/new-next.txt'),
      ],
    }
    render(
      <ApprovalCard
        conversation={initial}
        onChange={(change) => {
          authoritativeApproval = change(authoritativeApproval)
        }}
        onSubmit={vi.fn()}
      />,
    )

    fireEvent.click(within(screen.getByRole('group', { name: '审批项 1' })).getByRole('button', { name: '允许' }))

    expect(authoritativeApproval.items[0]?.interruptId).toBe('interrupt-new')
    expect(authoritativeApproval.items[0]?.decision).toBeUndefined()
  })

  it('keeps historical edit metadata readable without exposing an edit action', () => {
    const conversation = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      items: [approvalItem('history', '/history-result.txt', {
        allowedDecisions: ['approve', 'edit', 'reject'],
      })],
    })

    render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={vi.fn()} />)

    expect(screen.getByText('Write')).toBeInTheDocument()
    expect(screen.getByText('/history-result.txt')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument()
  })

  it('切换会话后始终显示当前审批动作', () => {
    const approval: ApprovalState = {
      activeIndex: 0,
      submitted: false,
      items: [approvalItem('collapse', '/collapse.txt')],
    }
    const conversation = conversationWithApproval(approval)
    const view = render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={vi.fn()} />)

    expect(screen.getAllByText('写入 /collapse.txt')).toHaveLength(1)
    expect(screen.queryByRole('separator', { name: '调整交互卡片高度' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /展开审批卡片|收起审批卡片/ })).not.toBeInTheDocument()
    view.rerender(
      <ApprovalCard
        conversation={conversationWithApproval(approval, 'thread-other')}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: '允许' })).toBeInTheDocument()
    expect(screen.queryByRole('separator', { name: '调整交互卡片高度' })).not.toBeInTheDocument()
  })

  it('announces an error and exposes an explicit retry for a completed group', () => {
    const onSubmit = vi.fn()
    const conversation = conversationWithApproval({
      activeIndex: 0,
      submitted: false,
      error: '审批提交失败',
      items: [approvalItem('retry', '/retry.txt', { decision: 'approved' })],
    })

    render(<ApprovalCard conversation={conversation} onChange={vi.fn()} onSubmit={onSubmit} />)

    expect(screen.getByRole('alert')).toHaveTextContent('审批提交失败')
    fireEvent.click(screen.getByRole('button', { name: '重新提交' }))
    expect(onSubmit).toHaveBeenCalledWith(['interrupt-retry'])
  })
})
