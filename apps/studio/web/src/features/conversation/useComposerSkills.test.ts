import { act, renderHook } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { listInstalledSkills } from '../skills/api'
import { subscribeResourceChanges } from '../../api/notifications'
import type { InstalledSkill } from '../skills/model'
import { useComposerSkills } from './useComposerSkills'

vi.mock('../skills/api', () => ({ listInstalledSkills: vi.fn() }))
vi.mock('../../api/notifications', () => ({ subscribeResourceChanges: vi.fn() }))
const first: InstalledSkill = { id: 'one', name: 'reports', description: 'Prepare reports', enabled: true, source_id: null, source_kind: 'zip', source_name: 'ZIP', external_id: null, author: null, topics: [], file_count: 1, byte_size: 64, created_at: '2026-01-01', updated_at: '2026-01-01' }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(subscribeResourceChanges).mockReturnValue(() => {}); vi.mocked(listInstalledSkills).mockResolvedValue([first, { ...first, id: 'two', name: 'research' }, { ...first, id: 'disabled', enabled: false }]) })

it('only offers enabled skills and retains selection until its own submission is accepted', async () => {
  const { result } = renderHook(() => useComposerSkills(true))
  await act(async () => {})
  expect(result.current.skills.map(item => item.id)).toEqual(['one', 'two'])
  act(() => result.current.choose('one'))
  const submitted = result.current.capture()
  expect(result.current.selected.map(item => item.id)).toEqual(['one'])
  act(() => result.current.acknowledge(submitted))
  expect(result.current.selected).toEqual([])
  act(() => result.current.choose('one'))
  const older = result.current.capture()
  act(() => { result.current.clear(); result.current.choose('two') })
  act(() => result.current.acknowledge(older))
  expect(result.current.selected.map(item => item.id)).toEqual(['two'])
})

it('exposes a recoverable loading error without dropping selected skills', async () => {
  const { result } = renderHook(() => useComposerSkills(true))
  await act(async () => {})
  act(() => result.current.choose('one'))
  vi.mocked(listInstalledSkills).mockRejectedValueOnce(new Error('offline'))
  act(() => result.current.retry()); await act(async () => {})
  expect(result.current.status).toBe('error')
  expect(result.current.selected[0]?.id).toBe('one')
  act(() => result.current.retry()); await act(async () => {})
  expect(result.current.status).toBe('ready')
})

it('aborts a catalog request when leaving the conversation and ignores its late result', async () => {
  let resolve!: (items: InstalledSkill[]) => void
  vi.mocked(listInstalledSkills).mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const { result, rerender } = renderHook(({ active }) => useComposerSkills(active), { initialProps: { active: true } })
  const signal = vi.mocked(listInstalledSkills).mock.calls[0][0]
  rerender({ active: false })
  expect(signal?.aborted).toBe(true)
  await act(async () => resolve([first]))
  expect(result.current.skills).toEqual([])
})

it('返回对话后重新核验列表，加载失败保留技能标签直到权威结果确认失效', async () => {
  const { result, rerender } = renderHook(({ active }) => useComposerSkills(active), { initialProps: { active: true } })
  await act(async () => {})
  act(() => result.current.choose('one'))
  rerender({ active: false })
  let reject: (reason: Error) => void = () => undefined
  vi.mocked(listInstalledSkills).mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
  rerender({ active: true })

  expect(result.current.status).toBe('loading')
  expect(result.current.selected).toMatchObject([{ id: 'one', name: 'reports' }])
  expect(result.current.selected[0].unavailable).toBeUndefined()
  await act(async () => reject(new Error('offline')))
  expect(result.current.status).toBe('error')
  expect(result.current.selected[0].unavailable).toBeUndefined()

  vi.mocked(listInstalledSkills).mockResolvedValue([{ ...first, enabled: false }])
  act(() => result.current.retry())
  await act(async () => {})
  expect(result.current.status).toBe('ready')
  expect(result.current.selected).toMatchObject([{ id: 'one', unavailable: true }])
})

it('marks a disabled selection after a notice and keeps it available for removal', async () => {
  const { result } = renderHook(() => useComposerSkills(true))
  await act(async () => {})
  act(() => result.current.choose('one'))
  vi.mocked(listInstalledSkills).mockResolvedValue([{ ...first, enabled: false }])
  const notice = vi.mocked(subscribeResourceChanges).mock.calls.at(-1)![0]
  await act(async () => notice({ kind: 'change', change: { topic: 'studio.skills.changed', key: 'disabled', scope: { namespace: 'ns_1', owner_id: null }, details: {} } }))
  expect(result.current.skills).toEqual([])
  expect(result.current.selected).toMatchObject([{ id: 'one', unavailable: true }])
  act(() => result.current.remove('one'))
  expect(result.current.selected).toEqual([])
})
