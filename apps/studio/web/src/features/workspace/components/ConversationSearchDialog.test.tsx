import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import { buildEmptyConversation } from '../../../lib/workspace'
import { ConversationSearchDialog } from './ConversationSearchDialog'
import type { ConversationSearchDialogProps } from './ConversationSearchDialog'

const first = { ...buildEmptyConversation({ projectId: 'first', now: '2030-01-01T00:00:00Z' }), threadId: 'one', title: '岗位调研' }
const second = { ...first, threadId: 'two', projectId: 'second', title: '薪酬报告' }
const base = (): ConversationSearchDialogProps => ({
  query: '报告', scope: 'project', results: [first, second], projectNames: { first: '研究项目', second: '产品项目' },
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

describe('会话搜索弹框', () => {
  it.each(['', '   '])('空白查询由占位文字提示，不展示或打开旧结果：%j', query => {
    const props = { ...base(), query, hasMore: true }
    render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    expect(input).toHaveAttribute('placeholder', '输入关键词')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.queryByText('方向键选择，Enter 打开，Esc 关闭')).not.toBeInTheDocument()
    expect(input).not.toHaveAttribute('aria-controls')
    expect(input).toHaveAttribute('aria-expanded', 'false')
    expect(input).not.toHaveAttribute('aria-activedescendant')
    expect(screen.queryByRole('option')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '加载更多' })).not.toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).not.toHaveBeenCalled()
  })

  it('打开后仅聚焦输入框，方向键选择结果后用Enter打开完整会话身份', () => {
    const props = base()
    render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    expect(input).toHaveFocus()
    expect(results().queryByRole('option', { selected: true })).not.toBeInTheDocument()
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('岗位调研')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('薪酬报告')
    expect(input).toHaveAttribute('aria-activedescendant', results().getByRole('option', { selected: true }).id)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).toHaveBeenCalledWith(second)
  })

  it('未选中时向上选择末项，方向键在首尾循环', () => {
    render(<ConversationSearchDialog {...base()} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('薪酬报告')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('岗位调研')
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expect(results().getByRole('option', { selected: true })).toHaveTextContent('薪酬报告')
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
      expect(results().queryByRole('option', { selected: true })).not.toBeInTheDocument()
      expect(props.onSelect).not.toHaveBeenCalled()
    }
  })

  it('范围列表保留自身方向键和Escape优先级，不导航搜索结果', () => {
    const props = base()
    render(<ConversationSearchDialog {...props} />)
    fireEvent.click(screen.getByRole('button', { name: '选择搜索范围' }))
    const picker = screen.getByRole('listbox', { name: '搜索范围' })
    fireEvent.keyDown(picker, { key: 'ArrowDown' })
    expect(results().queryByRole('option', { selected: true })).not.toBeInTheDocument()
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
    expect(props.onSelect).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).toHaveBeenCalledWith(first)
    rerender(<ConversationSearchDialog {...props} query="不存在" results={[]} />)
    expect(screen.getByRole('status')).toHaveTextContent('没有匹配的对话')
  })

  it('切换范围或选中结果被移除时不自动选中其他结果', () => {
    const props = base()
    const { rerender } = render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    rerender(<ConversationSearchDialog {...props} scope="all" />)
    expect(results().queryByRole('option', { selected: true })).not.toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    rerender(<ConversationSearchDialog {...props} scope="all" results={[second]} />)
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).not.toHaveBeenCalled()
  })

  it('清空后重新输入同一关键词不恢复先前选中项', () => {
    const props = base()
    const { rerender } = render(<ConversationSearchDialog {...props} />)
    const input = screen.getByRole('combobox', { name: '搜索会话' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.change(input, { target: { value: '' } })
    rerender(<ConversationSearchDialog {...props} query="" />)
    fireEvent.change(input, { target: { value: props.query } })
    rerender(<ConversationSearchDialog {...props} />)
    expect(results().queryByRole('option', { selected: true })).not.toBeInTheDocument()
    fireEvent.click(results().getByRole('option', { name: /薪酬报告/ }))
    expect(props.onSelect).toHaveBeenCalledWith(second)
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
