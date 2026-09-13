import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AutomationHistory } from './AutomationHistory'
import { AutomationRunDialog } from './AutomationRunDialog'
import { AUTOMATION_TEST_NOW, createAutomationFixture, runFixture } from '../../test/automationFixtures'
import { fetchRunDetail } from './api'

vi.mock('./api', () => ({ fetchRunDetail: vi.fn() }))
beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date(AUTOMATION_TEST_NOW)) })
afterEach(() => vi.useRealTimers())

function HistoryExample() {
  const [offset, setOffset] = useState(0)
  const [view, setView] = useState<'week' | 'list'>('week')
  return <AutomationHistory view={view} onViewChange={setView} cursors={{}} onLoadMore={vi.fn()} loading={false} runs={createAutomationFixture().runs} query="" status="all" onOpenRun={vi.fn()} offset={offset} onOffsetChange={setOffset} />
}

describe('自动化运行历史', () => {
  it('每天具有独立滚动区域，列表不提供再次执行操作', () => {
    render(<HistoryExample />)
    expect(screen.getAllByRole('region', { name: /的运行记录，可上下滚动/ })).toHaveLength(7)
    fireEvent.click(screen.getByRole('tab', { name: '列表' }))
    expect(screen.getByRole('tab', { name: '列表' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: /^执行 / })).not.toBeInTheDocument()
  })
  it('浏览过去周次后可返回当前周并恢复焦点', () => {
    render(<HistoryExample />)
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('9月7日 – 13日')
    fireEvent.click(screen.getByRole('button', { name: '上一周' }))
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('8月31日 – 9月6日')
    fireEvent.click(screen.getByRole('button', { name: '回到本周' }))
    expect(screen.getByRole('button', { name: '上一周' })).toHaveFocus()
  })
  it('结果对话框读取服务端事实且不提供审批或执行操作', async () => {
    const run = runFixture()
    vi.mocked(fetchRunDetail).mockResolvedValue({ ...run, resultAvailable: true, messages: [], attachments: [] })
    render(<AutomationRunDialog run={run} trigger={null} onClose={vi.fn()} />)
    await waitFor(() => expect(fetchRunDetail).toHaveBeenCalledWith(run.id, expect.any(AbortSignal)))
    expect(screen.queryByRole('button', { name: /执行|审批/ })).not.toBeInTheDocument()
  })
})
