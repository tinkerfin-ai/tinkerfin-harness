import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { requestJson } from '../../api/shared/http'
import { SettingsDialog } from './SettingsDialog'
import type { ServiceConfiguration, ServiceSettings } from './serviceSettings'

vi.mock('../../api/shared/http', async importOriginal => ({ ...await importOriginal<typeof import('../../api/shared/http')>(), requestJson: vi.fn() }))
vi.mock('./ModelOptionsEditor', () => { throw new Error('Editor resource unavailable') })

it('高级编辑器资源不可用时保留参数，并使用文本输入修正与保存', async () => {
  const configuration: ServiceConfiguration = { capability: 'web_search', provider_id: 'tavily', endpoint: 'https://api.tavily.com', extra: { topic: 'finance' }, request: null, depth: 'basic', max_results: 5 }
  const saved: ServiceSettings = { id: 'search', configuration, enabled: true, has_key: true, test_status: null, test_code: null, tested_at: null }
  vi.mocked(requestJson).mockImplementation(async (path, options) => {
    if (path === '/api/services/settings') return { web_search: saved, image_generation: null }
    const body = options?.body as { configuration: ServiceConfiguration }
    return { ...saved, configuration: body.configuration }
  })
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  try {
    const user = userEvent.setup()
    render(<SettingsDialog open user={{ user_id: 1, username: 'user', display_name: 'User', avatar_url: null, roles: [], disabled: false }} themePreference="light" onThemePreferenceChange={vi.fn()} onToast={vi.fn()} onClose={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: '服务连接' }))
    await screen.findByLabelText('API Key')
    await user.click(screen.getByText('搜索参数', { exact: true }))
    await user.click(screen.getByText('附加参数（JSON）', { exact: true }))
    await screen.findByText('高级编辑器不可用，可继续使用纯文本编辑')
    const input = screen.getByRole('textbox', { name: '高级参数 JSON' })
    expect(input).toHaveValue(JSON.stringify({ topic: 'finance' }, null, 2))
    fireEvent.change(input, { target: { value: '{invalid' } })
    expect(input).toHaveAttribute('aria-invalid', 'true')
    await user.click(screen.getByRole('button', { name: '保存' }))
    expect(vi.mocked(requestJson).mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(0)
    fireEvent.change(input, { target: { value: '{"topic":"economy"}' } })
    expect(input).toHaveAttribute('aria-invalid', 'false')
    await user.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByText('已保存', { exact: true })
    expect(requestJson).toHaveBeenCalledWith('/api/services/web_search', expect.objectContaining({ method: 'PUT', body: expect.objectContaining({ configuration: expect.objectContaining({ extra: { topic: 'economy' } }) }) }))
  } finally { consoleError.mockRestore() }
})
