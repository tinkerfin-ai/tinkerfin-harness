import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AutomationHistory } from './AutomationHistory'
import { AutomationRunDialog } from './AutomationRunDialog'
import { AUTOMATION_TEST_NOW, createAutomationFixture, runFixture } from '../../test/automationFixtures'
import { fetchRunDetail, type RunDetail } from './api'
import { mockResourceNotices } from '../../test/resourceNotices'
import automationStyles from './automation.css?raw'

vi.mock('./api', () => ({ fetchRunDetail: vi.fn() }))
beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date(AUTOMATION_TEST_NOW)) })
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

function HistoryExample() {
  const [offset, setOffset] = useState(0)
  const [view, setView] = useState<'week' | 'list'>('week')
  return <AutomationHistory view={view} onViewChange={setView} cursors={{}} onLoadMore={vi.fn()} loading={false} runs={createAutomationFixture().runs} query="" status="all" onOpenRun={vi.fn()} offset={offset} onOffsetChange={setOffset} />
}

describe('自动化运行历史', () => {
  it('顶栏与内容消费相同的页面边距令牌', () => {
    expect(automationStyles).toMatch(/\.automation-content\s*\{[^}]*padding:\s*var\(--space-6\) var\(--layout-page-gutter\);/s)
    expect(automationStyles).toMatch(/\.chat-header:has\(\.header-navigation\):has\(\.automation-header-actions\)\s*\{[^}]*padding-inline:\s*var\(--layout-page-gutter\);/s)
  })

  it('每天具有独立滚动区域，列表不提供再次执行操作', () => {
    render(<HistoryExample />)
    expect(screen.getAllByRole('region', { name: /的运行记录，可上下滚动/ })).toHaveLength(7)
    fireEvent.click(screen.getByRole('tab', { name: '列表' }))
    expect(screen.getByRole('tab', { name: '列表' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: /^执行 / })).not.toBeInTheDocument()
  })
  it('浏览过去周次后可返回当前周并恢复焦点', () => {
    render(<HistoryExample />)
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('9月7日 – 9月13日')
    fireEvent.click(screen.getByRole('button', { name: '上一周' }))
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('8月31日 – 9月6日')
    fireEvent.click(screen.getByRole('button', { name: '上一周' }))
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('8月24日 – 8月30日')
    fireEvent.click(screen.getByRole('button', { name: '回到本周' }))
    expect(screen.getByRole('button', { name: '上一周' })).toHaveFocus()
  })
  it('读取失败保留日期导航，不把未知结果显示为暂无记录', () => {
    const props = { runs: [], query: '', status: 'all' as const, onOpenRun: vi.fn(), offset: 0, onOffsetChange: vi.fn(), view: 'week' as const, onViewChange: vi.fn(), cursors: {}, onLoadMore: vi.fn(), loading: false, loadFailed: true }
    const { rerender } = render(<AutomationHistory {...props} />)
    expect(screen.getByRole('button', { name: '上一周' })).toBeVisible()
    expect(screen.queryByText('暂无记录')).not.toBeInTheDocument()
    rerender(<AutomationHistory {...props} view="list" />)
    expect(screen.queryByText('还没有运行记录')).not.toBeInTheDocument()
  })
  it('结果对话框读取服务端事实且不提供审批或执行操作', async () => {
    const run = runFixture()
    vi.mocked(fetchRunDetail).mockResolvedValue({ ...run, threadId: 'automation-thread', runId: 'automation-run', resultAvailable: true, messages: [], outputFiles: [{ id: 'report', name: 'report.md', mime_type: 'text/markdown', size_bytes: 12 }] })
    render(<AutomationRunDialog projectId="project-1" run={run} trigger={null} onToast={vi.fn()} onClose={vi.fn()} />)
    await waitFor(() => expect(fetchRunDetail).toHaveBeenCalledWith('project-1', run.id, expect.any(AbortSignal)))
    expect(await screen.findByRole('button', { name: 'report.md' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /执行|审批/ })).not.toBeInTheDocument()
  })
  it('结果读取失败通知全局提示，重试成功后恢复内容', async () => {
    const run = runFixture()
    const onToast = vi.fn()
    vi.mocked(fetchRunDetail).mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce({ ...run, threadId: 'automation-thread', runId: 'automation-run', resultAvailable: false, messages: [], outputFiles: [] })
    render(<AutomationRunDialog projectId="project-1" run={run} trigger={null} onToast={onToast} onClose={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('运行结果加载失败')
    expect(onToast).toHaveBeenCalledExactlyOnceWith('error', '运行结果加载失败')
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))
    expect(await screen.findByText('暂时没有可显示的结果')).toBeInTheDocument()
    expect(onToast).toHaveBeenCalledOnce()
  })
  it('重新读取失败保留已读取的结果和文件，重试期间仍可查看', async () => {
    const run = runFixture({ status: 'running', finishedAt: null })
    const detail = { ...run, threadId: 'automation-thread', runId: 'automation-run', resultAvailable: true, messages: [], outputFiles: [{ id: 'report', name: 'report.md', mime_type: 'text/markdown', size_bytes: 12 }] }
    vi.mocked(fetchRunDetail).mockResolvedValueOnce(detail).mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce(detail)
    const onToast = vi.fn()
    render(<AutomationRunDialog projectId="project-1" run={run} trigger={null} onToast={onToast} onClose={vi.fn()} />)
    expect(await screen.findByRole('button', { name: 'report.md' })).toBeInTheDocument()
    fireEvent(document, new Event('visibilitychange'))
    expect(await screen.findByRole('alert')).toHaveTextContent('运行结果加载失败')
    expect(screen.getByRole('button', { name: 'report.md' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))
    expect(screen.getByRole('button', { name: 'report.md' })).toBeVisible()
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(onToast).toHaveBeenCalledOnce()
  })
})


it('运行状态不变时刷新正文，终态后的迟到附件仍能显示', async () => {
  vi.useFakeTimers()
  const notices = mockResourceNotices()
  const run = runFixture({ status: 'running', finishedAt: null })
  const detail: RunDetail = {
    ...run, threadId: 'result-thread', runId: 'result-run', resultAvailable: true, outputFiles: [],
    messages: [{ id: 'reply', role: 'assistant', content: '第一段', traceSeq: 1, graphNamespace: [], runId: 'result-run', contentOmitted: false, status: 'streaming', createdAt: AUTOMATION_TEST_NOW, agui: null }],
  }
  vi.mocked(fetchRunDetail).mockReset().mockResolvedValue(detail)
  const { unmount } = render(<AutomationRunDialog projectId="project-1" run={run} trigger={null} onToast={vi.fn()} onClose={vi.fn()} />)
  await act(async () => vi.advanceTimersByTimeAsync(0))
  expect(screen.getByText('第一段')).toBeInTheDocument()
  vi.mocked(fetchRunDetail).mockResolvedValue({ ...detail, messages: [{ ...detail.messages[0], content: '完整正文' }] })
  act(() => notices.changed('trace.changed', 'result-thread'))
  await act(async () => vi.advanceTimersByTimeAsync(1999))
  expect(fetchRunDetail).toHaveBeenCalledTimes(1)
  await act(async () => vi.advanceTimersByTimeAsync(1))
  expect(screen.getByText('完整正文')).toBeInTheDocument()
  vi.mocked(fetchRunDetail).mockResolvedValue({ ...detail, status: 'succeeded' })
  act(() => notices.changed('automation.execution.changed', run.id))
  await act(async () => vi.advanceTimersByTimeAsync(2000))
  vi.mocked(fetchRunDetail).mockResolvedValue({ ...detail, status: 'succeeded', outputFiles: [{ id: 'late-file', name: 'late.md', mime_type: 'text/markdown', size_bytes: 20 }] })
  act(() => notices.changed('studio.attachments.changed', run.id, { collection_id: run.id }))
  await act(async () => vi.advanceTimersByTimeAsync(2000))
  expect(screen.getByRole('button', { name: 'late.md' })).toBeInTheDocument()
  unmount()
})
