import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { AgentModelCatalogItem } from '../../../api/models/types'
import { ComposerModelPicker } from './ComposerModelPicker'

const models: AgentModelCatalogItem[] = [
  { modelId: 'a', displayName: '同名模型', connectionId: 'first', connectionDisplayName: 'DeepSeek', imageSupport: 'unknown', reasoningEnabled: false, isDefault: true },
  { modelId: 'b', displayName: '同名模型', connectionId: 'second', connectionDisplayName: 'DeepSeek', imageSupport: 'unknown', reasoningEnabled: false, isDefault: false },
  { modelId: 'c', displayName: '另一个模型', connectionId: 'first', connectionDisplayName: 'DeepSeek', imageSupport: 'unknown', reasoningEnabled: false, isDefault: false },
]

function Example({ items = models }: { items?: AgentModelCatalogItem[] }) {
  const [open, setOpen] = useState(false)
  const [model, setModel] = useState('')
  return <ComposerModelPicker model={model} models={items} defaultModelId="a" status="ready"
    open={open} onOpenChange={setOpen} onSelectModel={setModel} onRetry={vi.fn()} />
}

describe('ComposerModelPicker', () => {
  it('按提供方标识分组，同名提供方及同名模型仍可独立选择', () => {
    render(<Example />)
    const trigger = screen.getByRole('button', { name: '选择模型' })
    expect(trigger).toHaveTextContent('同名模型')
    fireEvent.click(trigger)
    const groups = screen.getAllByRole('group', { name: 'DeepSeek' })
    expect(groups).toHaveLength(2)
    expect(within(groups[0]).getAllByRole('option')).toHaveLength(2)
    expect(within(groups[0]).getByRole('option', { name: '同名模型' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(within(groups[1]).getByRole('option', { name: '同名模型' }))
    expect(trigger).toHaveFocus()
    fireEvent.click(trigger)
    expect(within(screen.getAllByRole('group')[1]).getByRole('option')).toHaveAttribute('aria-selected', 'true')
  })

  it('分组展示顺序与跨组键盘选择一致，标题不参与选择', () => {
    render(<Example />)
    const trigger = screen.getByRole('button', { name: '选择模型' })
    fireEvent.click(trigger)
    const list = screen.getByRole('listbox', { name: '模型选项' })
    expect(screen.getAllByRole('option').map(option => option.textContent)).toEqual(['同名模型', '另一个模型', '同名模型'])
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    fireEvent.keyDown(list, { key: 'Enter' })
    expect(trigger).toHaveTextContent('另一个模型')
    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowDown' })
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Enter' })
    fireEvent.click(trigger)
    expect(within(screen.getAllByRole('group')[1]).getByRole('option')).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Home' })
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowUp' })
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Enter' })
    fireEvent.click(trigger)
    expect(within(screen.getAllByRole('group')[1]).getByRole('option')).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
    expect(trigger).toHaveFocus()
  })

  it('只有一个提供方时仍显示标题', () => {
    render(<Example items={[models[0]]} />)
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
    expect(screen.getByRole('group', { name: 'DeepSeek' })).toBeVisible()
  })

  it('加载时禁用选择，空目录与加载失败提供重试', () => {
    const retry = vi.fn()
    const props = { model: '', models: [], defaultModelId: '', open: false, onOpenChange: vi.fn(), onSelectModel: vi.fn(), onRetry: retry }
    const { rerender } = render(<ComposerModelPicker {...props} status="loading" />)
    expect(screen.getByRole('button', { name: '选择模型' })).toBeDisabled()
    rerender(<ComposerModelPicker {...props} status="empty" />)
    expect(screen.getByRole('status')).toHaveTextContent('未配置可用模型')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    rerender(<ComposerModelPicker {...props} status="error" />)
    fireEvent.click(screen.getByRole('button', { name: '重新加载模型' }))
    expect(retry).toHaveBeenCalledTimes(2)
  })
})
