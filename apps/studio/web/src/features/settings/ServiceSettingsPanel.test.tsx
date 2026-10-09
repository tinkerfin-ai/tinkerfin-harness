import { useLayoutEffect } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { requestJson } from '../../api/shared/http'
import { SettingsDialog } from './SettingsDialog'
import { ServiceSettingsPanel } from './ServiceSettingsPanel'
import { useServiceSettings } from './useServiceSettings'
import { emptyServices, type SavedServices, type ServiceCapability, type ServiceConfiguration } from './serviceSettings'

vi.mock('../../api/shared/http', async importOriginal => ({ ...await importOriginal<typeof import('../../api/shared/http')>(), requestJson: vi.fn() }))
let saved: SavedServices
let failSave: boolean
let testSignal: AbortSignal | undefined
let pendingTest: boolean

beforeEach(() => {
  saved = emptyServices(); failSave = false; testSignal = undefined; pendingTest = false
  vi.mocked(requestJson).mockReset()
  vi.mocked(requestJson).mockImplementation(async (path, options) => {
    if (path === '/api/services/settings') return structuredClone(saved)
    const capability = path.split('/')[3] as ServiceCapability
    if (path.endsWith('/test')) {
      testSignal = options?.signal
      if (pendingTest) return new Promise((_, reject) => options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
      return { outcome: 'success', code: 'success' }
    }
    if (failSave) throw new Error('failed')
    if (options?.method === 'DELETE') { saved[capability] = null; return null }
    const body = options?.body as { configuration: ServiceConfiguration; enabled: boolean; api_key: string | null }
    saved[capability] = { id: capability, configuration: body.configuration, enabled: body.enabled, has_key: Boolean(body.api_key) || Boolean(saved[capability]?.has_key), test_status: null, test_code: null, tested_at: null }
    return structuredClone(saved[capability])
  })
})

async function openServices(onClose = vi.fn()) {
  render(<SettingsDialog open user={{ user_id: 1, username: 'user', avatar_url: null, roles: [], disabled: false }} themePreference="light" onThemePreferenceChange={vi.fn()} onToast={vi.fn()} onClose={onClose} />)
  fireEvent.click(screen.getByRole('button', { name: '服务连接' }))
  await screen.findByLabelText('API Key')
  return onClose
}

it('每次打开服务页均在配置加载完成后提供编辑区，并保留独立草稿', async () => {
  const editableCommits: boolean[] = []
  const loads: Array<(value: SavedServices) => void> = []
  vi.mocked(requestJson).mockImplementation(() => new Promise<SavedServices>(resolve => loads.push(resolve)))
  function ServicePage({ active }: { active: boolean }) {
    const state = useServiceSettings(active)
    useLayoutEffect(() => {
      if (active) editableCommits.push(screen.queryByLabelText('API Key') !== null)
    })
    return active ? <ServiceSettingsPanel state={state} confirmClose={false} onCloseDecision={vi.fn()} /> : null
  }
  const view = render(<ServicePage active={false} />)
  view.rerender(<ServicePage active />)
  expect(editableCommits).not.toContain(true)
  expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
  await act(async () => loads[0](emptyServices()))
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'search-key' } })
  fireEvent.click(screen.getByRole('tab', { name: '图片生成' }))
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'image-model' } })
  fireEvent.click(screen.getByRole('tab', { name: '网页搜索' }))
  expect(screen.getByLabelText('API Key')).toHaveValue('search-key')
  fireEvent.click(screen.getByRole('tab', { name: '图片生成' }))
  expect(screen.getByLabelText('模型 ID')).toHaveValue('image-model')
  view.rerender(<ServicePage active={false} />)
  editableCommits.length = 0
  view.rerender(<ServicePage active />)
  expect(editableCommits).not.toContain(true)
  expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
  await act(async () => loads[1](emptyServices()))
  expect(screen.getByLabelText('模型 ID')).toHaveValue('')
})

it('两个能力直接编辑，独立保存且保存不调用测试', async () => {
  await openServices()
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'search-key' } })
  fireEvent.click(screen.getByRole('tab', { name: '图片生成' }))
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'image-model' } })
  fireEvent.click(screen.getByRole('button', { name: '通用' }))
  fireEvent.click(screen.getByRole('button', { name: '服务连接' }))
  expect(screen.getByLabelText('模型 ID')).toHaveValue('image-model')
  fireEvent.click(screen.getByRole('tab', { name: '网页搜索' }))
  expect(screen.getByLabelText('API Key')).toHaveValue('search-key')
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await screen.findByText('已保存', { exact: true })
  expect(saved.web_search?.configuration.provider_id).toBe('tavily')
  expect(saved.image_generation).toBeNull()
  expect(screen.getByLabelText('API Key')).toHaveValue('')
  expect(vi.mocked(requestJson).mock.calls.some(([path]) => path.endsWith('/test'))).toBe(false)
  expect(screen.queryByRole('button', { name: '添加服务' })).not.toBeInTheDocument()
})

it('主动测试只有一个按钮，停止会取消请求', async () => {
  pendingTest = true
  await openServices()
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'search-key' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await screen.findByText('已保存', { exact: true })
  fireEvent.click(screen.getByRole('button', { name: '测试搜索' }))
  const stop = await screen.findByRole('button', { name: '停止' })
  expect(screen.queryByRole('button', { name: '测试搜索' })).not.toBeInTheDocument()
  fireEvent.click(stop)
  expect(testSignal?.aborted).toBe(true)
  await screen.findByText('已停止等待，可能已消耗额度')
  expect(screen.getAllByRole('button', { name: '测试搜索' })).toHaveLength(1)
})

it('保存失败保留输入，关闭时在固定操作区确认未保存草稿', async () => {
  failSave = true
  const onClose = await openServices()
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'keep-key' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await screen.findByText('保存失败，请重试')
  expect(screen.getByLabelText('API Key')).toHaveValue('keep-key')
  fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
  await screen.findByText('有尚未保存的修改，关闭后将丢失')
  expect(onClose).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
  expect(screen.getByLabelText('API Key')).toHaveValue('keep-key')
  fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }))
  fireEvent.click(screen.getByRole('button', { name: '放弃修改并关闭' }))
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
})

it('清除期间阻止重复操作，失败就地反馈且保留配置可恢复焦点', async () => {
  await openServices()
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'search-key' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await screen.findByText('已保存', { exact: true })
  const original = vi.mocked(requestJson).getMockImplementation()!
  let rejectDelete: (error: Error) => void = () => { throw new Error('request not started') }
  vi.mocked(requestJson).mockImplementation((path, options) => options?.method === 'DELETE'
    ? new Promise((_, reject) => { rejectDelete = reject })
    : original(path, options))
  fireEvent.click(screen.getByRole('button', { name: '清除配置' }))
  expect(screen.getByRole('button', { name: '保留配置' })).toHaveFocus()
  fireEvent.click(screen.getByRole('button', { name: '清除配置' }))
  expect(screen.getByRole('button', { name: '保留配置' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '清除配置' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '关闭对话框' })).toBeDisabled()
  rejectDelete(new Error('delete failed'))
  await screen.findByText('保存失败，请重试')
  expect(screen.getByText('清除配置及密钥？此操作无法恢复')).toBeInTheDocument()
  expect(saved.web_search).not.toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '保留配置' }))
  expect(screen.getByRole('button', { name: '清除配置' })).toHaveFocus()
})

it('再次选择当前接入方式不会清空已编辑的地址和密钥', async () => {
  await openServices()
  fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://custom.example' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'keep-key' } })
  fireEvent.click(screen.getByRole('button', { name: '接入方式' }))
  fireEvent.click(screen.getByRole('option', { name: 'Tavily' }))
  expect(screen.getByLabelText('服务地址')).toHaveValue('https://custom.example')
  expect(screen.getByLabelText('API Key')).toHaveValue('keep-key')
})
