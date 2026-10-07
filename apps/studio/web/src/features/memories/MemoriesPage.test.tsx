import { createRef } from 'react'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoriesPage } from './MemoriesPage'
import * as api from './api'
import { applyResolvedLanguage, LANGUAGE_STORAGE_KEY, LocaleProvider } from '../../i18n'

vi.mock('./api', () => ({ listMemories: vi.fn(), readMemory: vi.fn(), saveMemory: vi.fn(), deleteMemory: vi.fn() }))
const memory = { path: '/research.md', content: '原内容', etag: 'a'.repeat(64), sizeBytes: 9, updatedAt: '2030-01-01', editable: true, preview: '原内容' }
const project = { id: 'project-1', name: '研究', createdAt: '2030-01-01', updatedAt: '2030-01-01' }
const setup = () => render(<MemoriesPage project={project} navigationTriggerRef={createRef()} onOpenNavigation={vi.fn()} onModalChange={vi.fn()} onToast={vi.fn()} />)
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.listMemories).mockResolvedValue({ items: [memory], nextOffset: null }); vi.mocked(api.readMemory).mockResolvedValue(memory) })
afterEach(() => { cleanup(); applyResolvedLanguage('zh-CN') })

it('保存冲突保留输入，对照最新内容后使用新条件提交', async () => {
  const latest = { ...memory, content: 'Agent 的修改', etag: 'b'.repeat(64) }
  vi.mocked(api.readMemory).mockResolvedValueOnce(memory).mockResolvedValueOnce(latest)
  vi.mocked(api.saveMemory).mockRejectedValueOnce(new Error('记忆已被修改')).mockResolvedValueOnce({ ...latest, content: '合并后的内容' })
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '编辑记忆：research.md' }))
  const dialog = await screen.findByRole('dialog', { name: '编辑记忆' })
  fireEvent.change(within(dialog).getByLabelText('内容'), { target: { value: '本地内容' } })
  fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
  expect(await within(dialog).findByText('Agent 的修改')).toBeVisible()
  expect(within(dialog).getByLabelText('内容')).toHaveValue('本地内容')
  fireEvent.change(within(dialog).getByLabelText('内容'), { target: { value: '合并后的内容' } })
  fireEvent.click(within(dialog).getByRole('button', { name: '以当前内容保存' }))
  expect(api.saveMemory).toHaveBeenLastCalledWith(project.id, memory.path, '合并后的内容', latest.etag, expect.any(AbortSignal))
})

it('关闭未保存的编辑需要选择是否放弃', async () => {
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '编辑记忆：research.md' }))
  const dialog = await screen.findByRole('dialog', { name: '编辑记忆' })
  fireEvent.change(within(dialog).getByLabelText('内容'), { target: { value: '尚未保存' } })
  fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
  expect(screen.getByLabelText('内容')).toHaveValue('尚未保存')
  expect(api.saveMemory).not.toHaveBeenCalled()
})

it.each(['新增记忆', '删除记忆：research.md'])('读取期间进入%s使旧编辑读取失效', async action => {
  let completeRead!: (value: typeof memory) => void
  vi.mocked(api.readMemory).mockImplementationOnce(() => new Promise(resolve => { completeRead = resolve }))
  setup()
  fireEvent.click(await screen.findByRole('button', { name: '编辑记忆：research.md' }))
  const signal = vi.mocked(api.readMemory).mock.calls[0][2]
  fireEvent.click(screen.getByRole('button', { name: action }))
  if (action === '新增记忆') {
    fireEvent.change(screen.getByLabelText('记忆名称'), { target: { value: 'new.md' } })
    fireEvent.change(screen.getByLabelText('内容'), { target: { value: '尚未保存的新内容' } })
  }
  await act(async () => completeRead(memory))
  expect(signal.aborted).toBe(true)
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  if (action === '新增记忆') {
    expect(screen.getByLabelText('记忆名称')).toHaveValue('new.md')
    expect(screen.getByLabelText('内容')).toHaveValue('尚未保存的新内容')
  } else expect(screen.getByRole('dialog')).toHaveAccessibleName('删除记忆')
})

it('英文界面显示记忆冲突的英文说明并保留输入', async () => {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en')
  vi.mocked(api.saveMemory).mockRejectedValueOnce(new Error('记忆已被修改，请重新读取后对照保存'))
  render(<LocaleProvider><MemoriesPage project={project} navigationTriggerRef={createRef()} onOpenNavigation={vi.fn()} onModalChange={vi.fn()} onToast={vi.fn()} /></LocaleProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Edit memory: research.md' }))
  const dialog = await screen.findByRole('dialog', { name: 'Edit memory' })
  fireEvent.change(within(dialog).getByLabelText('Content'), { target: { value: 'Local input' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Memory changed. Read the latest content and compare before saving')
  expect(within(dialog).getByLabelText('Content')).toHaveValue('Local input')
})
