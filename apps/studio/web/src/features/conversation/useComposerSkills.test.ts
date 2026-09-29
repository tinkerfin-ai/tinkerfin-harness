import { act, renderHook } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { listInstalledSkills } from '../skills/api'
import { subscribeResourceChanges } from '../../api/notifications'
import type { InstalledSkill } from '../skills/model'
import { useComposerSkills } from './useComposerSkills'
import { composerSkillReferences, createComposerDraft, insertComposerSkill } from './composerDraft'

vi.mock('../skills/api', () => ({ listInstalledSkills: vi.fn() }))
vi.mock('../../api/notifications', () => ({ subscribeResourceChanges: vi.fn() }))
const first: InstalledSkill = { id: 'one', name: 'reports', description: 'Prepare reports', enabled: true, source_id: null, source_kind: 'zip', source_name: 'ZIP', external_id: null, author: null, topics: [], file_count: 1, byte_size: 64, created_at: '2026-01-01', updated_at: '2026-01-01' }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(subscribeResourceChanges).mockReturnValue(() => {}); vi.mocked(listInstalledSkills).mockResolvedValue([first, { ...first, id: 'two', name: 'research' }, { ...first, id: 'disabled', enabled: false }]) })


const references = insertComposerSkill(createComposerDraft(), first).state.field(composerSkillReferences)

it('只提供启用的技能，已选身份来自草稿引用', async () => {
  const { result, rerender } = renderHook(({ selected }) => useComposerSkills(true, selected), { initialProps: { selected: references } })
  await act(async () => {})
  expect(result.current.skills.map(item => item.id)).toEqual(['one', 'two'])
  expect(result.current.selected.map(item => item.id)).toEqual(['one'])
  rerender({ selected: [] })
  expect(result.current.selected).toEqual([])
})

it('目录读取失败不删除草稿中的技能引用，重试后更新可用性', async () => {
  const { result } = renderHook(() => useComposerSkills(true, references))
  await act(async () => {})
  vi.mocked(listInstalledSkills).mockRejectedValueOnce(new Error('offline'))
  act(() => result.current.retry()); await act(async () => {})
  expect(result.current.status).toBe('error')
  expect(result.current.selected[0]?.id).toBe('one')
  act(() => result.current.retry()); await act(async () => {})
  expect(result.current.status).toBe('ready')
})

it('离开对话后取消目录请求，忽略迟到结果', async () => {
  let resolve!: (items: InstalledSkill[]) => void
  vi.mocked(listInstalledSkills).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const { result, rerender } = renderHook(({ active }) => useComposerSkills(active, []), { initialProps: { active: true } })
  const signal = vi.mocked(listInstalledSkills).mock.calls[0][0]
  rerender({ active: false })
  expect(signal?.aborted).toBe(true)
  await act(async () => resolve([first]))
  expect(result.current.skills).toEqual([])
})

it('返回对话后重新核验目录，停用或卸载的引用保留并标记不可用', async () => {
  const { result, rerender } = renderHook(({ active }) => useComposerSkills(active, references), { initialProps: { active: true } })
  await act(async () => {})
  rerender({ active: false })
  let reject: (reason: Error) => void = () => undefined
  vi.mocked(listInstalledSkills).mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
  rerender({ active: true })
  expect(result.current.status).toBe('loading')
  expect(result.current.selected[0].unavailable).toBeUndefined()
  await act(async () => reject(new Error('offline')))
  expect(result.current.status).toBe('error')
  expect(result.current.selected[0].unavailable).toBeUndefined()
  vi.mocked(listInstalledSkills).mockResolvedValue([{ ...first, enabled: false }])
  act(() => result.current.retry()); await act(async () => {})
  expect(result.current.selected).toMatchObject([{ id: 'one', unavailable: true }])
})

it('技能更新通知只影响可用性，不改写引用名称', async () => {
  const { result } = renderHook(() => useComposerSkills(true, references))
  await act(async () => {})
  vi.mocked(listInstalledSkills).mockResolvedValue([{ ...first, enabled: false }])
  const notice = vi.mocked(subscribeResourceChanges).mock.calls.at(-1)![0]
  await act(async () => notice({ kind: 'change', change: { topic: 'studio.skills.changed', key: 'disabled', scope: { namespace: 'ns_1', owner_id: null }, details: {} } }))
  expect(result.current.skills).toEqual([])
  expect(result.current.selected).toMatchObject([{ id: 'one', name: 'reports', unavailable: true }])
})
