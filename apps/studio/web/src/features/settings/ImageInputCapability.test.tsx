import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { requestJson } from '../../api/shared/http'
import { ImageInputCapabilityField } from './ImageInputCapability'
import type { ImageInputCapability, ModelConnection } from './useModelSettings'

vi.mock('../../api/shared/http', () => ({ requestJson: vi.fn() }))
const connection: ModelConnection = { connection_id: 'owned', display_name: 'OpenAI', provider_id: 'openai', api_type: 'openai_chat_completions', base_url: 'https://api.openai.com/v1', auth_type: 'api_key', has_key: true }
const supported: ImageInputCapability = { automatic: 'supported', effective: 'supported', source: 'catalog' }
const unknown: ImageInputCapability = { automatic: 'unknown', effective: 'unknown', source: 'unknown' }
const props = { connection, modelName: 'first', requestedName: 'first', declaration: 'unknown' as const, onChange: vi.fn() }

beforeEach(() => { vi.mocked(requestJson).mockReset(); props.onChange.mockReset() })

it('旧响应不能覆盖新的型号，卸载取消当前查询', async () => {
  let finishFirst!: (value: { image_input_capability: ImageInputCapability }) => void
  let finishSecond!: (value: { image_input_capability: ImageInputCapability }) => void
  vi.mocked(requestJson).mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }))
    .mockImplementationOnce(() => new Promise(resolve => { finishSecond = resolve }))
  const view = render(<ImageInputCapabilityField {...props} />)
  view.rerender(<ImageInputCapabilityField {...props} modelName="second" requestedName="second" />)
  expect(vi.mocked(requestJson).mock.calls[0][1]?.signal?.aborted).toBe(true)
  await act(async () => finishSecond({ image_input_capability: supported }))
  expect(screen.getByRole('button', { name: '图片输入' })).toHaveTextContent('自动 · 支持')
  await act(async () => finishFirst({ image_input_capability: unknown }))
  expect(screen.getByRole('button', { name: '图片输入' })).toHaveTextContent('自动 · 支持')
  view.unmount()
  expect(vi.mocked(requestJson).mock.calls[1][1]?.signal?.aborted).toBe(true)
})

it('未失焦的新输入不查询，失败明确呈现并可以重试', async () => {
  vi.mocked(requestJson).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ image_input_capability: unknown })
  const view = render(<ImageInputCapabilityField {...props} modelName="new" />)
  expect(requestJson).not.toHaveBeenCalled()
  view.rerender(<ImageInputCapabilityField {...props} modelName="new" requestedName="new" />)
  expect(await screen.findByText('自动 · 查询失败')).toBeVisible()
  expect(screen.getByText('能力查询失败，配置仍可保存')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findByText('自动 · 未识别')).toBeVisible()
})

it('已有人工声明可见且不会被自动结果覆盖', async () => {
  vi.mocked(requestJson).mockResolvedValue({ image_input_capability: { ...supported, effective: 'unsupported', source: 'manual' } })
  render(<ImageInputCapabilityField {...props} declaration="unsupported" />)
  expect(screen.getByRole('button', { name: '图片输入' })).toHaveTextContent('不支持')
  await waitFor(() => expect(requestJson).toHaveBeenCalledOnce())
  expect(screen.getByRole('button', { name: '图片输入' })).toHaveTextContent('不支持')
  fireEvent.click(screen.getByRole('button', { name: '图片输入' }))
  fireEvent.click(screen.getByRole('option', { name: '自动识别' }))
  expect(props.onChange).toHaveBeenCalledWith('unknown')
})
