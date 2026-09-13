import { describe, expect, it } from 'vitest'
import { beijingDate, emptyDraft, presentRun, validateDraft, weekDates } from './model'
import { runFixture } from '../../test/automationFixtures'

describe('自动化日历和输入', () => {
  it('默认完全访问并要求真实模型、名称和指令', () => {
    expect(emptyDraft().accessMode).toBe('full')
    expect(validateDraft(emptyDraft())).toEqual({ name: '请输入任务名称', prompt: '请输入任务指令', model: '请选择可用模型' })
  })
  it('日期按北京时间分组，周历跨月保持七天', () => {
    expect(beijingDate(Date.parse('2026-09-10T16:00:00Z'))).toBe('2026-09-11')
    expect(weekDates('2026-09-01')).toEqual(['2026-08-31', '2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-05', '2026-09-06'])
  })
  it('结果时间来自服务器，不把排队状态标记为完成', () => {
    const source = runFixture({ status: 'queued', startedAt: null, finishedAt: null })
    expect(presentRun(source)).toMatchObject({ status: 'queued', date: '2026-09-10', durationSeconds: 0 })
  })
  it('阻止无执行日、过长间隔和颠倒有效期', () => {
    const draft = { ...emptyDraft(), name: '任务', prompt: '整理', modelId: 'main' }
    expect(validateDraft({ ...draft, schedule: { kind: 'weekly', weekdays: [], time: '09:00' } })).toHaveProperty('schedule')
    expect(validateDraft({ ...draft, schedule: { kind: 'interval', every: 366, unit: 'days' } })).toHaveProperty('schedule')
    expect(validateDraft({ ...draft, startsOn: '2026-09-20', endsOn: '2026-09-10' })).toHaveProperty('validity')
  })
})
