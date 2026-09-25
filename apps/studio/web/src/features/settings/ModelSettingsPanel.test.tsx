import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelSettingsPanel } from './ModelSettingsPanel'
import { requestJson } from '../../api/shared/http'
import { newModel, type ConnectionWrite, type ModelConnection, type ModelSettings, type ProviderPreset } from './useModelSettings'

vi.mock('../../api/shared/http', () => ({ requestJson: vi.fn() }))
const connection: ModelConnection = { connection_id: 'shared', display_name: '我的服务', provider_id: 'custom', api_type: 'openai_chat_completions', base_url: 'https://api.example/v1', auth_type: 'api_key', has_key: true }
const presets: ProviderPreset[] = [
  { provider_id: 'custom', display_name: '自定义提供方', api_type: 'openai_chat_completions', base_url: '', auth_type: 'api_key', models: [] },
  { provider_id: 'dashscope', display_name: '通义千问', api_type: 'openai_chat_completions', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', auth_type: 'api_key', models: [] },
  { provider_id: 'ollama', display_name: 'Ollama', api_type: 'ollama', base_url: 'http://localhost:11434', auth_type: 'none', models: [] },
]
let models: ModelSettings[]
let connections: ModelConnection[]
let failWrite: boolean
let discoveryCode: string | undefined
function writes() { return vi.mocked(requestJson).mock.calls.filter(([, options]) => options?.method && options.method !== 'GET') }

beforeEach(() => {
  models = [{ ...newModel('shared'), model_id: 'mine', display_name: '我的模型', model_name: 'provider-model' }]
  connections = [connection]; failWrite = false; discoveryCode = undefined
  vi.mocked(requestJson).mockReset()
  vi.mocked(requestJson).mockImplementation(async (path, options) => {
    if (!options?.method) return { models, connections, providers: presets }
    if (failWrite) throw new Error('保存失败')
    if (path.endsWith('/models') && discoveryCode) return { outcome: 'failed', code: discoveryCode, items: [] }
    if (path.endsWith('/models')) return { outcome: 'success', code: 'models_received', items: [{ model_name: 'provider-model', display_name: 'provider-model' }, { model_name: 'new-model', display_name: '新模型' }] }
    if (path.endsWith('/default')) models = models.map(model => ({ ...model, is_default: true, enabled: true }))
    else if (path.includes('/connections/')) {
      const value = options.body as ConnectionWrite
      const saved = { ...value, has_key: value.auth_type === 'api_key' }
      connections = connections.some(item => item.connection_id === value.connection_id) ? connections.map(item => item.connection_id === value.connection_id ? saved : item) : [...connections, saved]
    } else if (options.method === 'POST') models = [...models, ...options.body as ModelSettings[]]
    else models = [options.body as ModelSettings]
    return null
  })
  HTMLElement.prototype.scrollIntoView = vi.fn()
})

describe('提供方与模型设置', () => {
  it('搜索名称或 Model ID 同步定位提供方与模型，清空后恢复选择', async () => {
    connections.push({ ...connection, connection_id: 'local', display_name: '本地服务' })
    models.push(
      { ...newModel('local'), model_id: 'qwen', display_name: '中文助手', model_name: 'qwen3:4b' },
      { ...newModel('local'), model_id: 'other', display_name: '其他模型', model_name: 'other-model' },
    )
    render(<ModelSettingsPanel />)
    const search = await screen.findByRole('textbox', { name: '搜索提供方或模型' })
    for (const query of [' QWEN3:4B ', '中文助手']) {
      fireEvent.change(search, { target: { value: query } })
      expect(screen.getByRole('heading', { name: '本地服务' })).toBeVisible()
      const list = screen.getByRole('region', { name: '模型列表' })
      expect(within(list).getByText('中文助手')).toBeVisible()
      expect(within(list).queryByText('其他模型')).not.toBeInTheDocument()
      expect(within(list).queryByText('我的模型')).not.toBeInTheDocument()
    }
    fireEvent.click(screen.getByRole('button', { name: '连接设置' }))
    fireEvent.click(screen.getByRole('button', { name: '删除提供方' }))
    expect(screen.getByRole('alert')).toHaveTextContent('删除提供方及其 2 个模型配置')
    fireEvent.click(within(screen.getByRole('navigation', { name: '模型配置导航' })).getByRole('button', { name: '模型配置' }))
    const restoredSearch = screen.getByRole('textbox', { name: '搜索提供方或模型' })
    fireEvent.change(restoredSearch, { target: { value: '本地服务' } })
    expect(within(screen.getByRole('region', { name: '模型列表' })).getByText('其他模型')).toBeVisible()
    fireEvent.change(restoredSearch, { target: { value: 'missing' } })
    expect(screen.getByText('没有匹配的提供方或模型')).toBeVisible()
    expect(screen.queryByRole('region', { name: '模型列表' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '清除搜索' }))
    expect(screen.getByRole('heading', { name: '我的服务' })).toBeVisible()
    expect(writes()).toHaveLength(0)
  })
  it('获取模型时按名称或 ID 搜索，隐藏的勾选项仍计数并一并添加', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '获取模型' }))
    await screen.findByRole('checkbox', { name: 'new-model' })
    const search = screen.getByRole('textbox', { name: '搜索模型名称或 Model ID' })
    fireEvent.change(search, { target: { value: ' NEW-MODEL ' } })
    expect(screen.queryByRole('checkbox', { name: /provider-model/ })).not.toBeInTheDocument()
    fireEvent.change(search, { target: { value: '新模型' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'new-model' }))
    fireEvent.change(search, { target: { value: 'provider-model' } })
    expect(screen.getByRole('checkbox', { name: /provider-model/ })).toBeDisabled()
    expect(screen.getByText('已选 1 项')).toBeVisible()
    fireEvent.change(search, { target: { value: 'missing' } })
    expect(screen.getByText('没有匹配的模型')).toBeVisible()
    expect(screen.queryByText('服务没有返回模型，可手动添加')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '清除搜索' }))
    expect(screen.getByRole('checkbox', { name: 'new-model' })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '添加所选模型' }))
    await waitFor(() => expect(writes()).toHaveLength(2))
    expect(writes()[1][1]?.body).toEqual([expect.objectContaining({ model_name: 'new-model', connection_id: 'shared', image_support: 'unknown' })])
  })
  it('面包屑展示提供方和当前模型，上级入口返回提供方模型列表', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '配置模型 我的模型' }))
    const navigation = screen.getByRole('navigation', { name: '模型配置导航' })
    expect(navigation).toHaveTextContent('模型配置我的服务我的模型')
    expect(screen.queryByText('返回模型列表')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '模型设置' })).not.toBeInTheDocument()
    fireEvent.click(within(navigation).getByRole('button', { name: '我的服务' }))
    expect(await screen.findByRole('button', { name: '获取模型' })).toBeVisible()
  })
  it('分栏支持方向键及最小最大宽度，模型列表有独立滚动区域', async () => {
    render(<ModelSettingsPanel />)
    const splitter = await screen.findByRole('separator', { name: '调整提供方列表宽度' })
    expect(screen.getByRole('region', { name: '模型列表' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: '模型配置' })).not.toBeInTheDocument()
    fireEvent.keyDown(splitter, { key: 'ArrowRight' })
    expect(splitter).toHaveAttribute('aria-valuenow', '188')
    fireEvent.keyDown(splitter, { key: 'Home' })
    expect(splitter.getAttribute('aria-valuenow')).toBe(splitter.getAttribute('aria-valuemin'))
    fireEvent.keyDown(splitter, { key: 'End' })
    fireEvent.keyDown(splitter, { key: 'ArrowRight' })
    expect(splitter.getAttribute('aria-valuenow')).toBe(splitter.getAttribute('aria-valuemax'))
  })
  it('连接共享凭据，编辑模型只提交连接引用和模型参数', async () => {
    const changed = vi.fn()
    render(<ModelSettingsPanel onChanged={changed} />)
    await screen.findByText('我的模型')
    expect(screen.getByText('已配置密钥')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '配置模型 我的模型' }))
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '重命名模型' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(writes()[0][1]?.body).toMatchObject({ connection_id: 'shared', display_name: '重命名模型' })
    expect(writes()[0][1]?.body).not.toHaveProperty('api_key')
    expect(writes()[0][1]?.body).not.toHaveProperty('base_url')
  })
  it('连接表单密钥留空表示保留数据库中的密钥', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '连接设置' }))
    expect(screen.getByLabelText('API Key')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '新的连接名' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes()[0][1]?.body).toMatchObject({ display_name: '新的连接名', api_key: null })
  })
  it('选择千问预设填写地址，密钥仍由表单输入', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '添加提供方' }))
    fireEvent.click(screen.getByRole('button', { name: '提供方' }))
    fireEvent.click(screen.getByRole('option', { name: '通义千问' }))
    expect(screen.getByLabelText('服务地址')).toHaveValue('https://dashscope.aliyuncs.com/compatible-mode/v1')
    expect(screen.getByLabelText('API Key')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'synthetic-test-key' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes()[0][1]?.body).toMatchObject({ provider_id: 'dashscope', api_key: 'synthetic-test-key' })
    expect(writes()[0][1]?.body).not.toHaveProperty('models')
  })
  it('Ollama 预设无需输入占位密钥，也不展示未支持协议', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '添加提供方' }))
    fireEvent.click(screen.getByRole('button', { name: '提供方' }))
    fireEvent.click(screen.getByRole('option', { name: 'Ollama' }))
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: '无需认证' })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'API 类型' }))
    expect(screen.queryByText(/Responses|Anthropic/)).not.toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(writes()).toHaveLength(1))
    expect(writes()[0][1]?.body).toMatchObject({ api_type: 'ollama', auth_type: 'none', api_key: '' })
  })
  it('写入失败保留模型草稿，重试时仍使用相同模型身份', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '手动添加' }))
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '新模型' } })
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'new-model' } })
    failWrite = true
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(writes()).toHaveLength(1))
    await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeEnabled())
    expect(screen.getByLabelText('显示名称')).toHaveValue('新模型')
    failWrite = false
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(writes()).toHaveLength(2))
    expect(writes()[1][0]).toBe(writes()[0][0])
  })
  it('发现模型后选择添加，已有模型不重复写入', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '获取模型' }))
    expect(await screen.findByRole('checkbox', { name: /provider-model/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'new-model' }))
    fireEvent.click(screen.getByRole('button', { name: '添加所选模型' }))
    await waitFor(() => expect(writes()).toHaveLength(2))
    expect(writes()[1][0]).toBe('/api/models/configurations')
    expect(writes()[1][1]?.body).toEqual([expect.objectContaining({ model_name: 'new-model', connection_id: 'shared', image_support: 'unknown' })])
  })
  it.each([
    ['authentication_failed', '服务拒绝了认证，请检查密钥及接口权限'],
    ['timeout', '获取模型列表超时，请重试'],
    ['response_too_large', '模型列表超过大小限制'],
  ])('模型发现失败保留原因与重试入口：%s', async (code, message) => {
    discoveryCode = code
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '获取模型' }))
    expect(await screen.findByText(message)).toBeVisible()
    expect(screen.getByRole('button', { name: '手动添加' })).toBeEnabled()
    discoveryCode = undefined
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByRole('checkbox', { name: 'new-model' })).toBeEnabled()
    expect(screen.queryByText(message)).not.toBeInTheDocument()
  })
  it('设默认不要求再次输入密钥', async () => {
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '设为默认' }))
    await screen.findByText('默认', { exact: true })
    expect(writes()[0][0]).toBe('/api/models/configurations/mine/default')
    expect(writes()[0][1]?.body).toBeUndefined()
  })
})
