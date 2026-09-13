import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { TodoGroup } from '../../../../api/conversation/taskTrace'
import { TodoTraceDrawer } from './TodoTraceDrawer'
import todoTraceStyles from '../todoTrace.css?raw'

const group = (index: number): TodoGroup => ({
  id: `todo-group:run-${index}`,
  userMessageId: `message-${index}`,
  userMessagePreview: index === 0 ? '最新任务' : `历史任务 ${index}`,
  groupToolCallId: `tool-${index}`,
  createdAt: new Date(Date.UTC(2026, 7, 31, 0, 0, index)).toISOString(),
  status: index === 0 ? 'running' : 'completed',
  todos: [{
    id: `todo-${index}`,
    content: `任务内容 ${index}`,
    status: index === 0 ? 'running' : 'completed',
  }],
})

describe('TodoTraceDrawer', () => {
  it('defaults to the latest group, allows independent expansion, and locates', async () => {
    const locate = vi.fn()
    render(
      <TodoTraceDrawer
        groups={[group(0), group(1)]}
        open
        usesOverlay={false}
        openEpoch={1}
        drawerRef={createRef()}
        onClose={vi.fn()}
        onLocate={locate}
      />,
    )

    const latest = await screen.findByRole('button', { name: '收起任务组：最新任务' })
    expect(latest).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('heading', { name: '当前' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '历史' })).toBeInTheDocument()
    expect(screen.getByText('当前会话 · 2 组')).toBeInTheDocument()
    expect(within(latest).getAllByText('最新任务')[0]).toHaveClass('todo-trace-group-title')
    expect(within(latest).queryByText('1 项')).not.toBeInTheDocument()
    expect(latest.querySelector('.todo-trace-group-state')).not.toBeInTheDocument()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(screen.getByText('任务内容 0')).toBeInTheDocument()

    fireEvent.click(latest)
    expect(screen.getByRole('button', { name: '展开任务组：最新任务' }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('任务内容 0')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '展开任务组：最新任务' }))
    expect(screen.getByRole('button', { name: '收起任务组：最新任务' }))
      .toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('任务内容 0')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '展开任务组：历史任务 1' }))
    expect(screen.getByRole('button', { name: '收起任务组：最新任务' }))
      .toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: '收起任务组：历史任务 1' }))
      .toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('任务内容 1')).toBeInTheDocument()
    const historyList = screen.getAllByRole('list', { name: '任务列表' })
      .find((list) => within(list).queryByText('任务内容 1'))
    expect(within(historyList as HTMLElement).queryByText('已完成'))
      .not.toBeInTheDocument()
    expect(screen.getByText('任务内容 0')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '定位到对话：历史任务 1' }))
    expect(locate).toHaveBeenCalledWith(expect.objectContaining({ id: 'todo-group:run-1' }))
  })

  it('renders one task-trace title without a Trace eyebrow', () => {
    render(
      <TodoTraceDrawer
        groups={[group(0)]}
        open
        usesOverlay={false}
        openEpoch={1}
        drawerRef={createRef()}
        onClose={vi.fn()}
        onLocate={vi.fn()}
      />,
    )

    expect(screen.getAllByRole('heading', { name: '任务轨迹' })).toHaveLength(1)
    expect(screen.queryByText('Trace', { exact: true })).not.toBeInTheDocument()
  })

  it('keeps completed-only groups in collapsed history without a current section', () => {
    render(
      <TodoTraceDrawer
        groups={[group(1), group(2)]}
        open
        usesOverlay={false}
        openEpoch={1}
        drawerRef={createRef()}
        onClose={vi.fn()}
        onLocate={vi.fn()}
      />,
    )

    expect(screen.queryByRole('heading', { name: '当前' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '历史' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '展开任务组：历史任务 1' }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('任务内容 1')).not.toBeInTheDocument()
  })

  it('uses borderless surfaces, icon-free roots, and aligned progress treatment', () => {
    expect(todoTraceStyles).toMatch(/\.todo-trace-drawer\s*{[^}]*border:\s*0;[^}]*background:\s*var\(--color-canvas\);/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-group-surface\s*{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-group\.is-current \.todo-trace-group-surface\s*{[^}]*linear-gradient\(/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-group\.is-expanded\.is-history \.todo-trace-group-surface\s*{[^}]*linear-gradient\(/s)
    expect(todoTraceStyles).not.toContain('.todo-trace-group-state')
    expect(todoTraceStyles).toMatch(/\.todo-trace-todo\.is-completed \.todo-trace-node-icon\s*{[^}]*color:\s*var\(--color-text-secondary\);/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-todo\.is-completed \.todo-trace-node-icon\s*{[^}]*background:\s*transparent;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-completed-mark\s*{[^}]*box-shadow:\s*none;[^}]*filter:\s*none;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-drawer\s*{[^}]*grid-template-rows:\s*var\(--layout-drawer-header-height\) minmax\(0, 1fr\);/s)
    expect(todoTraceStyles).not.toContain('.todo-trace-drawer-head')
    expect(todoTraceStyles).toMatch(/\.todo-trace-group-title\s*{[^}]*grid-row:\s*1;[^}]*align-self:\s*center;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-group-progress\s*{[^}]*grid-row:\s*1;[^}]*align-self:\s*center;[^}]*padding:\s*0;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-group-panel\s*{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
    expect(todoTraceStyles).not.toContain('.todo-trace-group-tooltip')
    expect(todoTraceStyles).toMatch(/\.todo-trace-locate:hover\s*{[^}]*background:\s*transparent;/s)
    expect(todoTraceStyles).toMatch(/\.todo-trace-locate-target\s*{[^}]*outline:\s*1px solid var\(--color-focus\);[^}]*outline-offset:\s*0;/s)
    const statusRule = todoTraceStyles.match(/\.todo-trace-status-label\s*{([^}]*)}/s)?.[1] ?? ''
    expect(statusRule).not.toMatch(/background|border|padding/)
  })

  it('supports Arrow, Home, End, and Page keyboard movement across roots', async () => {
    render(
      <TodoTraceDrawer
        groups={[group(0), group(1), group(2)]}
        open
        usesOverlay={false}
        openEpoch={1}
        drawerRef={createRef()}
        onClose={vi.fn()}
        onLocate={vi.fn()}
      />,
    )
    const latest = await screen.findByRole('button', { name: '收起任务组：最新任务' })
    latest.focus()
    fireEvent.keyDown(latest, { key: 'End' })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '展开任务组：历史任务 2' }))
        .toHaveFocus()
    })
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Home' })
    await waitFor(() => expect(latest).toHaveFocus())
  })

  it('windows a five-thousand-group tree to a bounded number of root rows', async () => {
    render(
      <TodoTraceDrawer
        groups={Array.from({ length: 5_000 }, (_, index) => group(index))}
        open
        usesOverlay={false}
        openEpoch={1}
        drawerRef={createRef()}
        onClose={vi.fn()}
        onLocate={vi.fn()}
      />,
    )

    await screen.findByRole('button', { name: '收起任务组：最新任务' })
    expect(screen.getAllByRole('listitem').length).toBeLessThanOrEqual(80)
    expect(screen.getByRole('list', { name: '任务列表' })).toBeInTheDocument()
  })

  it('removes the closed drawer from interaction and accessibility', () => {
    render(
      <TodoTraceDrawer
        groups={[group(0)]}
        open={false}
        usesOverlay
        openEpoch={0}
        drawerRef={createRef()}
        onClose={vi.fn()}
        onLocate={vi.fn()}
      />,
    )

    const drawer = screen.getByLabelText('任务轨迹', { selector: 'aside' })
    expect(drawer).toHaveAttribute('aria-hidden', 'true')
    expect(drawer).toHaveAttribute('inert')
  })
})
