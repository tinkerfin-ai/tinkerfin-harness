import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createRef } from 'react'
import { subscribeResourceChanges } from '../../api/notifications'
import * as api from './api'
import { SkillsPage } from './SkillsPage'
import type { InstalledSkill, RemoteSkill, RemoteSkillPage } from './model'

vi.mock('./api', () => ({
  listSkillSources: vi.fn(), listInstalledSkills: vi.fn(), browseSkills: vi.fn(), readRemoteSkill: vi.fn(),
  readInstalledSkill: vi.fn(), installSkill: vi.fn(), setSkillEnabled: vi.fn(), uninstallSkill: vi.fn(), updateSkill: vi.fn(),
  previewGitHubSkills: vi.fn(), previewZipSkills: vi.fn(), confirmSkillImport: vi.fn(),
}))
vi.mock('../../api/notifications', () => ({ subscribeResourceChanges: vi.fn() }))
const remote: RemoteSkill = { id: 'author/reports', source_id: 'clawhub', name: 'Reports', description: 'Write verified reports', revision: 'fixed', author: 'Author', topics: ['Writing'], updated_at: null }
const installed: InstalledSkill = {project_id: 'project-1', overridden: false,  id: 'installed', name: 'reports', description: 'Write verified reports', source_id: 'clawhub', source_kind: 'catalog', source_name: 'ClawHub', external_id: remote.id, enabled: true, author: 'Author', topics: ['Writing'], file_count: 2, byte_size: 128, created_at: '2026-01-01', updated_at: '2026-01-01' }
const detail = { name: 'reports', description: 'Write verified reports', markdown: '# Reports\nUse sources', files: ['SKILL.md', 'scripts/run.py'], author: 'Author', source_url: 'https://clawhub.ai/author/reports', topics: ['Writing'] }

beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); history.replaceState(null, '', '/?page=skills')
  vi.mocked(subscribeResourceChanges).mockReturnValue(() => {})
  vi.mocked(api.listSkillSources).mockResolvedValue([{ id: 'clawhub', name: 'ClawHub', url: 'https://clawhub.ai' }])
  vi.mocked(api.listInstalledSkills).mockResolvedValue([installed])
  vi.mocked(api.browseSkills).mockResolvedValue({ items: [remote], cursor: null })
  vi.mocked(api.readInstalledSkill).mockResolvedValue(detail)
  vi.mocked(api.readRemoteSkill).mockResolvedValue({ skill: remote, detail })
})
afterEach(() => { vi.useRealTimers() })
const flush = async () => { await act(async () => {}); await act(async () => { await vi.runOnlyPendingTimersAsync() }) }
function setup() {
  render(<SkillsPage project={{ id: 'project-1', name: '测试项目', createdAt: '2030-01-01', updatedAt: '2030-01-01' }} navigationTriggerRef={createRef()} onOpenNavigation={vi.fn()} onModalChange={vi.fn()} onToast={vi.fn()} />)
}

it('shows a permanent source choice and keeps state controls in Mine with a read-only detail', async () => {
  setup(); await flush()
  expect(within(screen.getByRole('navigation', { name: '技能来源' })).getByRole('button', { name: 'ClawHub' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByRole('button', { name: '技能状态' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '技能排序' })).not.toBeInTheDocument()
  expect(screen.queryByText('最近更新', { exact: true })).not.toBeInTheDocument()
  expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看技能：Reports' })); await flush()
  const drawer = screen.getByRole('dialog')
  expect(within(drawer).getByRole('heading', { level: 1, name: 'Reports' })).toBeInTheDocument()
  expect(within(drawer).getByText('Use sources', { exact: true })).toBeInTheDocument()
  expect(within(drawer).queryByRole('switch')).not.toBeInTheDocument()
  expect(within(drawer).queryByRole('button', { name: /安装|卸载/ })).not.toBeInTheDocument()
  fireEvent.click(within(drawer).getByRole('button', { name: '关闭' }))
  fireEvent.click(screen.getByRole('tab', { name: '我的' })); await flush()
  expect(screen.getByRole('switch', { name: '启用技能：reports' })).toHaveAttribute('aria-checked', 'true')
  fireEvent.click(screen.getByRole('button', { name: '管理技能：reports' }))
  expect(screen.getAllByRole('menuitem').map(item => item.textContent)).toEqual(['详情', '更新', '卸载'])
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
  fireEvent.click(screen.getByRole('button', { name: '技能排序' }))
  expect(screen.getAllByRole('option').map(item => item.textContent)).toEqual(['最近更新', '按名称'])
  expect(document.querySelector('select')).toBeNull()
})

it('ignores a late source response and preserves searches separately for each source', async () => {
  vi.mocked(api.listSkillSources).mockResolvedValue([{ id: 'clawhub', name: 'ClawHub', url: 'https://clawhub.ai' }, { id: 'team', name: 'Team', url: 'https://team.example' }])
  let resolveFirst!: (page: RemoteSkillPage) => void
  vi.mocked(api.browseSkills).mockImplementation((source, query) => source === 'clawhub' && query === 'first'
    ? new Promise(resolve => { resolveFirst = resolve })
    : Promise.resolve({ items: [{ ...remote, source_id: source, name: source === 'team' ? 'Team skill' : 'Reports' }], cursor: null }))
  setup(); await flush()
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'first' } }); await flush()
  fireEvent.click(screen.getByRole('button', { name: 'Team' })); await flush()
  expect(screen.getByRole('searchbox')).toHaveValue('')
  expect(screen.getByRole('button', { name: '查看技能：Team skill' })).toBeInTheDocument()
  await act(async () => resolveFirst({ items: [{ ...remote, name: 'Late result' }], cursor: null }))
  expect(screen.queryByText('Late result')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'ClawHub' }))
  expect(screen.getByRole('searchbox')).toHaveValue('first')
})

it('keeps the previous toggle state when the command fails and allows another attempt', async () => {
  vi.mocked(api.setSkillEnabled).mockRejectedValueOnce(new Error('offline')).mockImplementationOnce(async () => {
    vi.mocked(api.listInstalledSkills).mockResolvedValue([{ ...installed, enabled: false }])
    return { ...installed, enabled: false }
  })
  setup(); await flush()
  fireEvent.click(screen.getByRole('tab', { name: '我的' })); await flush()
  fireEvent.click(screen.getByRole('switch')); await flush()
  expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true')
  expect(screen.getByRole('alert')).toHaveTextContent('操作失败，请重试')
  fireEvent.click(screen.getByRole('switch')); await flush()
  expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
})

it('keeps a confirmed installation when an earlier library request arrives late', async () => {
  let resolveInitial!: (items: InstalledSkill[]) => void
  vi.mocked(api.listInstalledSkills).mockImplementationOnce(() => new Promise(resolve => { resolveInitial = resolve }))
  vi.mocked(api.installSkill).mockResolvedValue(installed)
  setup(); await flush()
  fireEvent.click(screen.getByRole('button', { name: '安装技能：Reports' })); await flush()
  expect(screen.getByText('已安装', { exact: true })).toBeInTheDocument()
  await act(async () => resolveInitial([]))
  expect(screen.getByText('已安装', { exact: true })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '我的' })); await flush()
  expect(screen.getByRole('article', { name: 'reports' })).toBeInTheDocument()
})

it('offers a reset when a saved category no longer has installed skills', async () => {
  history.replaceState(null, '', '/?page=skills&skillView=mine&skillCategory=Missing')
  setup(); await flush()
  expect(screen.queryByRole('article')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '清除筛选' })); await flush()
  expect(screen.getByRole('article', { name: 'reports' })).toBeInTheDocument()
  expect(screen.getByRole('searchbox')).toHaveValue('')
})

it('submits only the selected previewed packages and retains the preview after failure', async () => {
  vi.mocked(api.previewGitHubSkills).mockResolvedValue({ id: 'draft', source: 'github', candidates: [
    { digest: 'one', name: 'one', description: 'First', file_count: 1, byte_size: 32 },
    { digest: 'two', name: 'two', description: 'Second', file_count: 2, byte_size: 64 },
  ] })
  vi.mocked(api.confirmSkillImport).mockRejectedValueOnce(new Error('offline'))
  setup(); await flush()
  fireEvent.click(screen.getByRole('button', { name: '导入技能' }))
  fireEvent.change(screen.getByLabelText('GitHub 地址'), { target: { value: 'https://github.com/author/repository' } })
  fireEvent.click(screen.getByRole('button', { name: '预览技能' })); await flush()
  fireEvent.click(screen.getByRole('checkbox', { name: /two/ }))
  fireEvent.click(screen.getByRole('button', { name: '安装 1 项技能' })); await flush()
  expect(api.confirmSkillImport).toHaveBeenCalledWith('project-1', 'draft', ['one'], expect.any(String), expect.any(AbortSignal))
  expect(screen.getByRole('checkbox', { name: /one/ })).toHaveAttribute('aria-checked', 'true')
  expect(screen.getByRole('alert')).toHaveTextContent('导入失败，请重试')
})

it('updates a remote skill through its menu and reuses the operation ID after failure', async () => {
  vi.mocked(api.updateSkill).mockRejectedValueOnce(new Error('offline')).mockImplementationOnce(async () => {
    const next = { ...installed, description: 'Updated instructions', enabled: false }
    vi.mocked(api.listInstalledSkills).mockResolvedValue([next])
    return { installation: next, changed: true }
  })
  setup(); await flush()
  fireEvent.click(screen.getByRole('tab', { name: '我的' })); await flush()
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const trigger = screen.getByRole('button', { name: '管理技能：reports' })
    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
    const update = screen.getByRole('menuitem', { name: '更新' })
    expect(update).toHaveFocus()
    fireEvent.click(update)
    expect(trigger).toHaveFocus()
    await flush()
    if (!attempt) expect(screen.getByRole('alert')).toHaveTextContent('操作失败，请重试')
  }
  const calls = vi.mocked(api.updateSkill).mock.calls
  expect(calls[0][2]).toBe(calls[1][2])
  expect(screen.getByText('Updated instructions')).toBeVisible()
  expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('replaces a ZIP installation with its same-name preview and preserves failed input', async () => {
  const zip = { ...installed, source_kind: 'zip' as const, source_id: null, source_name: 'ZIP', external_id: null }
  vi.mocked(api.listInstalledSkills).mockResolvedValue([zip])
  vi.mocked(api.previewZipSkills).mockResolvedValue({ id: 'zip-draft', source: 'zip', candidates: [{ digest: 'replacement', name: 'reports', description: 'New ZIP', file_count: 3, byte_size: 150 }] })
  vi.mocked(api.updateSkill).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ installation: zip, changed: true })
  setup(); await flush()
  fireEvent.click(screen.getByRole('tab', { name: '我的' })); await flush()
  fireEvent.click(screen.getByRole('button', { name: '管理技能：reports' }))
  fireEvent.click(screen.getByRole('menuitem', { name: '更新' }))
  const dialog = screen.getByRole('dialog', { name: '更新「reports」' })
  expect(dialog).toContainElement(document.activeElement as HTMLElement)
  fireEvent.change(within(dialog).getByLabelText('ZIP 文件'), { target: { files: [new File(['zip'], 'reports.zip', { type: 'application/zip' })] } })
  fireEvent.click(within(dialog).getByRole('button', { name: '预览技能' })); await flush()
  expect(within(dialog).getByText('New ZIP')).toBeVisible()
  expect(within(dialog).queryByRole('checkbox')).not.toBeInTheDocument()
  fireEvent.click(within(dialog).getByRole('button', { name: '更新' })); await flush()
  expect(within(dialog).getByRole('alert')).toHaveTextContent('更新失败，请重试')
  expect(within(dialog).getByText('New ZIP')).toBeVisible()
  fireEvent.click(within(dialog).getByRole('button', { name: '更新' })); await flush()
  const calls = vi.mocked(api.updateSkill).mock.calls
  expect(calls[0][2]).toBe(calls[1][2])
  expect(calls[0][3]).toEqual({ draft_id: 'zip-draft', digest: 'replacement' })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('refreshes installed cards after a chat mutation notice', async () => {
  setup(); await flush()
  fireEvent.click(screen.getByRole('tab', { name: '我的' })); await flush()
  vi.mocked(api.listInstalledSkills).mockResolvedValue([{ ...installed, description: 'Changed from chat' }])
  const notice = vi.mocked(subscribeResourceChanges).mock.calls.at(-1)![0]
  await act(async () => notice({ kind: 'change', change: { topic: 'studio.skills.changed', key: 'request', scope: { namespace: 'ns_1', owner_id: null }, details: {} } }))
  expect(screen.getByText('Changed from chat')).toBeVisible()
})

it('keeps the resolved release and operation ID when retrying an unconfirmed install', async () => {
  vi.mocked(api.listInstalledSkills).mockResolvedValue([])
  vi.mocked(api.browseSkills).mockResolvedValue({ items: [{ ...remote, revision: null }], cursor: null })
  vi.mocked(api.installSkill).mockRejectedValueOnce(new Error('offline')).mockImplementationOnce(async () => {
    vi.mocked(api.listInstalledSkills).mockResolvedValue([installed])
    return installed
  })
  setup(); await flush()
  fireEvent.click(screen.getByRole('button', { name: '安装技能：Reports' })); await flush()
  vi.mocked(api.readRemoteSkill).mockResolvedValue({ skill: { ...remote, revision: 'later' }, detail })
  fireEvent.click(screen.getByRole('button', { name: '安装技能：Reports' })); await flush()
  expect(api.readRemoteSkill).toHaveBeenCalledTimes(1)
  const calls = vi.mocked(api.installSkill).mock.calls
  expect(calls[0].slice(0, 5)).toEqual(calls[1].slice(0, 5))
  expect(calls[1][3]).toBe('fixed')
})
