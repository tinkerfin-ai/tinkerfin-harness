import { fireEvent, render, screen } from '@testing-library/react'
import { ListChecks } from 'lucide-react'
import { describe, expect, it, vi } from 'vitest'

import type { Message } from '../../../types'
import { ToolCallRow } from './ToolCallRow'

const message = (toolName: string): Message => ({
  id: `tool-${toolName}`,
  role: 'tool',
  content: toolName,
  createdAt: '2026-08-30T12:00:00.000Z',
  meta: {
    toolName,
    toolCallId: `tool-${toolName}`,
    params: '{"path":"/workspace/report.md"}',
    status: 'completed',
  },
})

describe('ToolCallRow presentation', () => {
  it('没有详情时呈现静态行，详情到达后才提供展开入口', () => {
    const { rerender } = render(<ToolCallRow message={message('read_file')}>{null}</ToolCallRow>)
    expect(screen.getByText('Read').closest('details')).toBeNull()
    expect(screen.getByText('/workspace/report.md')).toBeInTheDocument()

    rerender(<ToolCallRow message={message('read_file')}><div>读取结果</div></ToolCallRow>)
    expect(screen.getByText('Read').closest('summary')).not.toBeNull()
  })

  it('澄清提问用表单标题作为摘要', () => {
    const params = JSON.stringify({ form: {
      title: '发布上线计划澄清',
      description: '确认发布环境',
      questions: [{ id: 'environment', answerType: 'text', prompt: '发布到哪个环境？', required: true }],
    } }, null, 2)
    const question = message('ask_user_question')
    render(<ToolCallRow message={{ ...question, meta: { ...question.meta, params } }}><div>参数详情</div></ToolCallRow>)

    expect(screen.getByText('提问')).toBeInTheDocument()
    expect(screen.getByText('发布上线计划澄清')).toBeInTheDocument()
    expect(screen.queryByText(/ask_user_question|\{/)).not.toBeInTheDocument()
  })

  it.each([
    '{\n  "options": {"enabled": true}\n}',
    '[{"id": 1}]',
    '{',
    '{"query":"尚未完整',
  ])('结构化参数没有可读摘要时只显示工具名称：%s', (params) => {
    const custom = message('custom_tool')
    render(<ToolCallRow message={{ ...custom, meta: { ...custom.meta, params } }}><div>参数详情</div></ToolCallRow>)

    expect(screen.getByText('Tool call')).toBeInTheDocument()
    expect(screen.getByText('custom_tool')).toBeInTheDocument()
    expect(screen.queryByText(/[{}]|\[|\]/)).not.toBeInTheDocument()
  })

  it('renders a controlled Todos presentation without exposing tool arguments', () => {
    render(
      <ToolCallRow
        message={message('write_todos')}
        presentationOverride={{
          title: 'Todos',
          summary: '1/2',
          icon: <ListChecks size={14} />,
        }}
      >
        <div>任务树</div>
      </ToolCallRow>,
    )

    expect(screen.getByText('Todos', { selector: '.tool-row-title' }))
      .toBeInTheDocument()
    expect(screen.getByText('1/2')).toBeInTheDocument()
    expect(screen.queryByText('/workspace/report.md')).not.toBeInTheDocument()
  })

  it('uses Todos for an ordinary write_todos tool without exposing arguments', () => {
    render(
      <ToolCallRow message={message('write_todos')}>
        <div>任务详情</div>
      </ToolCallRow>,
    )

    expect(screen.getByText('Todos', { selector: '.tool-row-title' }))
      .toBeInTheDocument()
    expect(screen.queryByText('/workspace/report.md')).not.toBeInTheDocument()
  })

  it('keeps the ordinary Tool presentation unchanged', () => {
    render(
      <ToolCallRow message={message('read_file')}>
        <div>普通详情</div>
      </ToolCallRow>,
    )

    expect(screen.getByText('Read')).toBeInTheDocument()
    expect(screen.getByText('/workspace/report.md')).toBeInTheDocument()
  })

  it('reveals an opened row with the nearest scroll position and does not scroll on close', () => {
    const onOpenChange = vi.fn()
    const frame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    const { container } = render(
      <ToolCallRow message={message('glob')} onOpenChange={onOpenChange}>
        <div>展开详情</div>
      </ToolCallRow>,
    )
    const row = container.querySelector<HTMLDetailsElement>('.tool-row')!
    const scrollIntoView = vi.fn()
    row.scrollIntoView = scrollIntoView

    row.open = true
    fireEvent(row, new Event('toggle'))

    expect(onOpenChange).toHaveBeenLastCalledWith(true)
    expect(scrollIntoView).toHaveBeenCalledOnce()
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'nearest' })

    row.open = false
    fireEvent(row, new Event('toggle'))

    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    expect(scrollIntoView).toHaveBeenCalledOnce()
    frame.mockRestore()
  })
})
