import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { mockNativePopover } from '../../test/nativePopover'
import { useComposerDraft } from '../conversation/useComposerDraft'
import { ProjectSwitcher } from './ProjectSwitcher'
import { ProjectsWorkspace, type ProjectWorkspaceScope } from './ProjectsWorkspace'
import * as api from './api'

vi.mock('./api', () => ({ listProjects: vi.fn(), saveProject: vi.fn() }))
const first = { id: 'first', name: '研究', createdAt: '2030-01-01', updatedAt: '2030-01-01' }
const second = { ...first, id: 'second', name: '开发' }
const user = { user_id: 1, username: 'one', display_name: '用户', avatar_url: null, roles: [], disabled: false }
function Editor({ scope }: { scope: ProjectWorkspaceScope }) {
  const draft = useComposerDraft('', { key: scope.project.id, store: scope.drafts })
  return <><ProjectSwitcher scope={scope} /><input aria-label="草稿" value={draft.text} onChange={event => draft.setText(event.target.value)} /></>
}
const setup = () => render(<ProjectsWorkspace user={user} onLogout={vi.fn()}>{scope => <Editor key={scope.project.id} scope={scope} />}</ProjectsWorkspace>)
let restorePopover: () => void
beforeEach(() => {
  restorePopover = mockNativePopover()
  vi.clearAllMocks(); localStorage.clear(); window.history.replaceState({}, '', '/'); vi.mocked(api.listProjects).mockResolvedValue([first, second]) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); restorePopover() })

it('项目切换分别保留输入，浏览器返回恢复项目范围', async () => {
  setup()
  await screen.findByRole('button', { name: '切换项目：研究' })
  fireEvent.change(screen.getByRole('textbox', { name: '草稿' }), { target: { value: '研究草稿' } })
  fireEvent.click(screen.getByRole('button', { name: '切换项目：研究' }))
  fireEvent.click(screen.getByRole('button', { name: '开发' }))
  expect(screen.getByRole('textbox', { name: '草稿' })).toHaveValue('')
  fireEvent.change(screen.getByRole('textbox', { name: '草稿' }), { target: { value: '开发草稿' } })
  act(() => { window.history.replaceState({}, '', '/?project=first'); window.dispatchEvent(new PopStateEvent('popstate')) })
  expect(screen.getByRole('textbox', { name: '草稿' })).toHaveValue('研究草稿')
  fireEvent.click(screen.getByRole('button', { name: '切换项目：研究' }))
  fireEvent.click(screen.getByRole('button', { name: '开发' }))
  expect(screen.getByRole('textbox', { name: '草稿' })).toHaveValue('开发草稿')
})

it('创建失败保留名称，成功后直接进入新项目', async () => {
  vi.mocked(api.listProjects).mockResolvedValue([])
  vi.mocked(api.saveProject).mockRejectedValueOnce(new Error('项目名称已存在')).mockResolvedValueOnce(first)
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '创建项目' }))
  const dialog = screen.getByRole('dialog')
  fireEvent.change(within(dialog).getByLabelText('项目名称'), { target: { value: first.name } })
  fireEvent.click(within(dialog).getByRole('button', { name: '确认' }))
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('项目名称已存在')
  expect(within(dialog).getByLabelText('项目名称')).toHaveValue(first.name)
  vi.mocked(api.listProjects).mockResolvedValue([first])
  fireEvent.click(within(dialog).getByRole('button', { name: '确认' }))
  expect(await screen.findByRole('button', { name: '切换项目：研究' })).toBeVisible()
  expect(new URLSearchParams(location.search).get('project')).toBe(first.id)
})

it('空白名称提交定位到错误字段，修改后清除错误且可重新校验', async () => {
  const interaction = userEvent.setup()
  vi.mocked(api.listProjects).mockResolvedValue([])
  setup()
  await interaction.click(await screen.findByRole('button', { name: '创建项目' }))
  const dialog = screen.getByRole('dialog')
  const name = within(dialog).getByRole('textbox', { name: '项目名称' })
  const confirm = within(dialog).getByRole('button', { name: '确认' })
  await interaction.type(name, '   ')
  await interaction.click(confirm)
  expect(name).toHaveFocus()
  expect(name).toHaveAttribute('aria-invalid', 'true')
  expect(within(dialog).getByRole('alert')).toHaveTextContent('请输入项目名称')
  expect(api.saveProject).not.toHaveBeenCalled()
  await interaction.type(name, '研究')
  expect(name).toHaveAttribute('aria-invalid', 'false')
  expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
  await interaction.clear(name)
  await interaction.click(confirm)
  expect(name).toHaveFocus()
  expect(within(dialog).getByRole('alert')).toHaveTextContent('请输入项目名称')
  await interaction.click(within(dialog).getByRole('button', { name: '取消' }))
  await interaction.click(screen.getByRole('button', { name: '创建项目' }))
  expect(screen.getByRole('textbox', { name: '项目名称' })).toHaveAttribute('aria-invalid', 'false')
})

it('同一回合重复提交只保存一次，失败保留行内名称并允许重试', async () => {
  let fail!: (reason: Error) => void
  const pending = new Promise<api.Project>((_resolve, reject) => { fail = reject })
  vi.mocked(api.saveProject).mockReturnValueOnce(pending).mockResolvedValueOnce({ ...first, name: '新研究' })
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '切换项目：研究' }))
  fireEvent.click(screen.getByRole('button', { name: '重命名项目' }))
  fireEvent.change(screen.getByRole('textbox', { name: '项目名称' }), { target: { value: '新研究' } })
  const form = screen.getByRole('form', { name: '重命名项目' }) as HTMLFormElement
  act(() => { form.requestSubmit(); form.requestSubmit() })
  expect(api.saveProject).toHaveBeenCalledOnce()
  expect(screen.getByRole('textbox', { name: '项目名称' })).toBeDisabled()
  await act(async () => { fail(new Error('项目名称已存在')); await pending.catch(() => {}) })
  expect(screen.getByRole('textbox', { name: '项目名称' })).toHaveValue('新研究')
  expect(screen.getByRole('alert')).toHaveTextContent('项目名称已存在')
  vi.mocked(api.listProjects).mockResolvedValue([{ ...first, name: '新研究' }, second])
  fireEvent.submit(screen.getByRole('form', { name: '重命名项目' }))
  expect(await screen.findByRole('button', { name: '切换项目：新研究' })).toBeVisible()
})

it('浏览器返回到另一项目时撤销未提交编辑，不把旧名称放入当前项目', async () => {
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '切换项目：研究' }))
  fireEvent.click(screen.getByRole('button', { name: '开发' }))
  fireEvent.click(screen.getByRole('button', { name: '切换项目：开发' }))
  fireEvent.click(screen.getByRole('button', { name: '重命名项目' }))
  fireEvent.change(screen.getByRole('textbox', { name: '项目名称' }), { target: { value: '开发草稿' } })
  act(() => { window.history.replaceState({}, '', '/?project=first'); window.dispatchEvent(new PopStateEvent('popstate')) })
  fireEvent.click(screen.getByRole('button', { name: '切换项目：研究' }))
  expect(screen.queryByRole('textbox', { name: '项目名称' })).not.toBeInTheDocument()
  expect(api.saveProject).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '重命名项目' }))
  expect(screen.getByRole('textbox', { name: '项目名称' })).toHaveValue('研究')
})

it('项目变化取消在途保存，迟到的结果不覆盖当前范围', async () => {
  let finish!: (project: api.Project) => void
  const pending = new Promise<api.Project>(resolve => { finish = resolve })
  vi.mocked(api.saveProject).mockReturnValue(pending)
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '切换项目：研究' }))
  fireEvent.click(screen.getByRole('button', { name: '重命名项目' }))
  fireEvent.change(screen.getByRole('textbox', { name: '项目名称' }), { target: { value: '旧项目名称' } })
  fireEvent.submit(screen.getByRole('form', { name: '重命名项目' }))
  const signal = vi.mocked(api.saveProject).mock.calls[0][2]!
  act(() => { window.history.replaceState({}, '', '/?project=second'); window.dispatchEvent(new PopStateEvent('popstate')) })
  expect(signal.aborted).toBe(true)
  await act(async () => { finish({ ...first, name: '旧项目名称' }); await pending })
  expect(screen.getByRole('button', { name: '切换项目：开发' })).toBeVisible()
  expect(screen.queryByRole('textbox', { name: '项目名称' })).not.toBeInTheDocument()
})
