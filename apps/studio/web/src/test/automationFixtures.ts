import { presentRun, type AutomationRun, type AutomationRunRecord, type AutomationTask } from '../features/automation/model'

export const AUTOMATION_TEST_NOW = '2026-09-10T07:00:00Z'
export const taskFixture = (overrides: Partial<AutomationTask> = {}): AutomationTask => ({
  id: 'news', name: '每日 AI 新闻简报', prompt: '整理新闻', modelId: 'main', accessMode: 'full',
  schedule: { kind: 'daily', time: '09:00' }, startsOn: '', endsOn: '', enabled: true,
  attachments: [], files: [], revision: 1, nextRunAt: '2026-09-11T01:00:00Z', ...overrides,
})
export const runFixture = (overrides: Partial<AutomationRunRecord> = {}): AutomationRun => presentRun({
  id: 'run', taskId: 'news', name: '每日 AI 新闻简报', queuedAt: '2026-09-10T01:00:00Z', startedAt: '2026-09-10T01:00:01Z', finishedAt: '2026-09-10T01:00:05Z', status: 'succeeded', trigger: 'scheduled', error: null, ...overrides,
})
export function createAutomationFixture(): { tasks: AutomationTask[]; runs: AutomationRun[] } {
  const tasks = [
    taskFixture(),
    taskFixture({ id: 'jobs', name: '企业与岗位动态日报', schedule: { kind: 'daily', time: '20:00' } }),
    taskFixture({ id: 'portfolio', name: '投资组合收盘复盘', schedule: { kind: 'workdays', time: '15:30' } }),
    taskFixture({ id: 'weekly', name: '每周工作总结', schedule: { kind: 'weekly', weekdays: [4], time: '18:00' } }),
    taskFixture({ id: 'products', name: '竞品产品动态追踪', enabled: false, nextRunAt: null, schedule: { kind: 'interval', every: 2, unit: 'hours' } }),
  ]
  const rows: Array<[string, string, string, AutomationRun['status']]> = [
    ['2026-09-10', '14:25', 'portfolio', 'succeeded'], ['2026-09-10', '11:10', 'products', 'succeeded'], ['2026-09-10', '09:00', 'news', 'succeeded'],
    ['2026-09-09', '20:00', 'jobs', 'succeeded'], ['2026-09-09', '18:05', 'products', 'failed'], ['2026-09-09', '15:30', 'portfolio', 'succeeded'],
    ['2026-09-09', '12:00', 'products', 'succeeded'], ['2026-09-09', '10:00', 'products', 'succeeded'], ['2026-09-09', '09:00', 'news', 'failed'],
    ['2026-09-08', '20:00', 'jobs', 'succeeded'], ['2026-09-08', '18:00', 'products', 'succeeded'], ['2026-09-08', '15:30', 'portfolio', 'succeeded'],
    ['2026-09-08', '12:00', 'products', 'succeeded'], ['2026-09-08', '09:00', 'news', 'succeeded'],
    ['2026-09-07', '20:00', 'jobs', 'succeeded'], ['2026-09-07', '18:00', 'products', 'succeeded'], ['2026-09-07', '15:30', 'portfolio', 'succeeded'],
    ['2026-09-07', '12:00', 'products', 'succeeded'], ['2026-09-07', '10:00', 'products', 'succeeded'], ['2026-09-07', '09:00', 'news', 'succeeded'],
    ['2026-09-04', '18:00', 'weekly', 'succeeded'],
  ]
  const runs = rows.map(([date, time, taskId, status], index) => runFixture({
    id: `run-${index}`, taskId, name: tasks.find(task => task.id === taskId)!.name,
    queuedAt: `${date}T${time}:00+08:00`, startedAt: `${date}T${time}:01+08:00`, finishedAt: `${date}T${time}:43+08:00`, status,
    error: status === 'failed' ? '资讯来源暂时无法访问，本次未生成完整结果' : null,
  }))
  return { tasks, runs }
}
