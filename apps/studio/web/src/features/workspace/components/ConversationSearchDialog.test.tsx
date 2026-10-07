import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import { buildEmptyConversation } from '../../../lib/workspace'
import { ConversationSearchDialog } from './ConversationSearchDialog'
import type { ConversationSearchDialogProps } from './ConversationSearchDialog'

const first = { ...buildEmptyConversation({ projectId: 'first', now: '2030-01-01T00:00:00Z' }), threadId: 'one', title: '岗位调研' }
const second = { ...first, threadId: 'two', projectId: 'second', title: '薪酬报告' }
const base = (): ConversationSearchDialogProps => ({
  query: '', scope: 'project', results: [first, second], projectNames: { first: '研究项目', second: '产品项目' },
  loading: false, loadingMore: false, error: null, hasMore: false, restoreFocusTo: null,
  onQueryChange: vi.fn(), onScopeChange: vi.fn(), onSelect: vi.fn(), onLoadMore: vi.fn(), onRetry: vi.fn(), onClose: vi.fn(),
})

const results = () => within(screen.getByRole('listbox', { name: '会话搜索结果' }))

const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
beforeAll(() => Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() }))
afterAll(() => {
  if (originalScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScroll)
  else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
})

describe('居中会话搜索', () => {
  it('打开后聚焦全局输入框，方向键选择并用Enter打开完整会话身份', () => {
    const props = base()
    render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    expect(input).toHaveFocus()
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('岗位调研')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('薪酬报告')
    expect(input).toHaveAttribute('aria-activedescendant', results().getByRole('option', { selected: true }).id)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).toHaveBeenCalledWith(second)
  })

  it.each([
    { signal: '组合输入状态', nativeFields: { isComposing: true } },
    { signal: '输入法键码', nativeFields: { keyCode: 229 } },
  ])('通过$signal标记的候选选择与确认不会导航或打开会话', ({ nativeFields }) => {
    const props = base()
    render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    fireEvent.compositionStart(input)

    for (const key of ['ArrowDown', 'ArrowUp', 'Enter']) {
      expect(fireEvent.keyDown(input, { key, ...nativeFields })).toBe(true)
      expect(results().getByRole('option', { selected: true })).toHaveTextContent('岗位调研')
      expect(props.onSelect).not.toHaveBeenCalled()
    }
  })

  it('范围列表保留自身方向键和Escape优先级，不导航搜索结果', () => {
    const props = base()
    render(<ConversationSearchDialog {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '选择搜索范围' }))
    const picker = screen.getByRole('listbox', { name: '搜索范围' })
    fireEvent.keyDown(picker, { key: 'ArrowDown' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('岗位调研')
    fireEvent.keyDown(picker, { key: 'Enter' })
    expect(props.onScopeChange).toHaveBeenCalledWith('all')
    expect(props.onClose).not.toHaveBeenCalled()
  })

  it('查询变化、加载和空结果不能打开上一次选中的结果', () => {
    const props = base()
    const { rerender } = render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    rerender(<ConversationSearchDialog {...props} query="岗位" loading results={[]} />)
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('正在搜索会话')
    rerender(<ConversationSearchDialog {...props} query="岗位" results={[first]} />)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).toHaveBeenCalledWith(first)
    rerender(<ConversationSearchDialog {...props} query="不存在" results={[]} />)
    expect(screen.getByRole('status')).toHaveTextContent('没有匹配的对话')
  })

  it('错误提供重试，分页加载时禁用重复操作', () => {
    const props = base()
    const { rerender } = render(<ConversationSearchDialog {...props} error="搜索会话失败" results={[]} />)
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(props.onRetry).toHaveBeenCalledOnce()
    rerender(<ConversationSearchDialog {...props} hasMore />)
    fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
    expect(props.onLoadMore).toHaveBeenCalledOnce()
    rerender(<ConversationSearchDialog {...props} hasMore loadingMore />)
    expect(screen.getByRole('button', { name: '加载更多' })).toBeDisabled()
  })

  it('Escape和全局关闭按钮均交由宿主关闭', () => {
    const props = base()
    render(<ConversationSearchDialog {...props} />)
    fireEvent.keyDown(screen.getByRole('combobox', { name: '搜索会话' }), { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
    expect(props.onClose).toHaveBeenCalledTimes(2)
  })
})
