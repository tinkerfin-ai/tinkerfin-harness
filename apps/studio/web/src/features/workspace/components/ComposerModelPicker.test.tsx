import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { AgentModelCatalogItem } from '../../../api/models/types'
import { ComposerModelPicker } from './ComposerModelPicker'

it('目录移除当前模型时保留选择权，主动选择后恢复有效显示', () => {
  const models: AgentModelCatalogItem[] = [
    { modelId: 'main', displayName: '当前模型', connectionId: 'provider', connectionDisplayName: '提供方', reasoningEnabled: false, isDefault: false },
    { modelId: 'available', displayName: '可用模型', connectionId: 'provider', connectionDisplayName: '提供方', reasoningEnabled: false, isDefault: true },
  ]
  const props = { model: 'main', models, defaultModelId: 'available', status: 'ready' as const,
    open: false, onOpenChange: vi.fn(), onSelectModel: vi.fn(), onRetry: vi.fn() }
  const view = render(<ComposerModelPicker {...props} />)
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('当前模型')
  const available = models.slice(1)
  view.rerender(<ComposerModelPicker {...props} models={available} />)
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('请选择可用模型')
  expect(props.onSelectModel).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '选择模型' }))
  expect(props.onOpenChange).toHaveBeenCalledWith(true)
  view.rerender(<ComposerModelPicker {...props} models={available} open />)
  const option = within(screen.getByRole('listbox', { name: '模型选项' })).getByRole('option', { name: /可用模型/ })
  expect(option).toHaveAttribute('aria-selected', 'false')
  fireEvent.click(option)
  expect(props.onSelectModel).toHaveBeenCalledExactlyOnceWith('available')
  view.rerender(<ComposerModelPicker {...props} models={available} model="available" />)
  expect(screen.getByRole('button', { name: '选择模型' })).toHaveTextContent('可用模型')
})
