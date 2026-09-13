import type { AccessMode } from '../../types'
import type { Attachment } from '../conversation/attachments/content'

/** 日期与时间按北京时间展示，不依赖浏览器时区 */
export const AUTOMATION_TIME_ZONE = 'Asia/Shanghai'
const DAY_MS = 86_400_000

export type AutomationSchedule =
  | { kind: 'once'; date: string; time: string }
  | { kind: 'daily' | 'workdays'; time: string }
  | { kind: 'weekly'; weekdays: number[]; time: string }
  | { kind: 'monthly'; day: number; time: string }
  | { kind: 'interval'; every: number; unit: 'minutes' | 'hours' | 'days' }

/** 服务端保存的任务与日程，附件ID用于提交，files用于展示 */
export interface AutomationTask {
  id: string
  name: string
  prompt: string
  schedule: AutomationSchedule
  enabled: boolean
  startsOn: string
  endsOn: string
  modelId: string
  accessMode: AccessMode
  attachments: string[]
  files: Attachment[]
  revision: number
  nextRunAt: string | null
}

export const runStatusLabels = {
  queued: '排队中', running: '运行中', interrupted: '等待人工处理', cancel_requested: '正在取消',
  succeeded: '运行完成', failed: '运行失败', timed_out: '运行超时', cancelled: '已取消', needs_attention: '结果待确认',
} as const
export type RunStatus = keyof typeof runStatusLabels

/** 时间展示只从服务端事实推导，不模拟执行或生成结果 */
export interface AutomationRunRecord {
  id: string
  taskId: string | null
  name: string | null
  queuedAt: string
  startedAt: string | null
  finishedAt: string | null
  status: RunStatus
  trigger: string
  error: string | null
}
export interface AutomationRun extends Omit<AutomationRunRecord, 'name'> {
  name: string
  date: string
  time: string
  durationSeconds: number
}
export interface AutomationDraft {
  name: string
  prompt: string
  schedule: AutomationSchedule
  startsOn: string
  endsOn: string
  modelId: string
  accessMode: AccessMode
  attachments: string[]
  files: Attachment[]
}
export type DraftField = 'name' | 'prompt' | 'schedule' | 'validity' | 'model'
export type DraftError = '请输入任务名称' | '请输入任务指令' | '请输入有效的执行时间'
  | '请选择有效的执行日期' | '请至少选择一个执行日' | '请选择每月 1–31 日'
  | '请输入 1–999 的执行间隔' | '请选择有效的起止日期' | '结束日期不能早于开始日期'
  | '执行日期必须在有效期内' | '请选择可用模型' | '执行间隔不得超过365天'
export type DraftErrors = Partial<Record<DraftField, DraftError>>

export function isCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}

export function shiftDate(date: string, days: number): string {
  return new Date(Date.parse(`${date}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10)
}

export function weekDates(date: string, offset = 0): string[] {
  const weekday = (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7
  const monday = shiftDate(date, -weekday + offset * 7)
  return Array.from({ length: 7 }, (_, index) => shiftDate(monday, index))
}

export function beijingDate(timestamp: number): string {
  return new Date(timestamp + 8 * 3_600_000).toISOString().slice(0, 10)
}

export function beijingTime(timestamp: number): string {
  return new Date(timestamp + 8 * 3_600_000).toISOString().slice(11, 16)
}


export function validateDraft(draft: AutomationDraft): DraftErrors {
  const errors: DraftErrors = {}
  if (!draft.modelId) errors.model = '请选择可用模型'
  if (!draft.name.trim()) errors.name = '请输入任务名称'
  if (!draft.prompt.trim()) errors.prompt = '请输入任务指令'
  const schedule = draft.schedule
  if (schedule.kind === 'interval') {
    if (!Number.isInteger(schedule.every) || schedule.every < 1 || schedule.every > 999) {
      errors.schedule = '请输入 1–999 的执行间隔'
    }
    if (schedule.unit === 'days' && schedule.every > 365) errors.schedule = '执行间隔不得超过365天'
  } else {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(schedule.time)) errors.schedule = '请输入有效的执行时间'
    if (schedule.kind === 'once' && !isCalendarDate(schedule.date)) errors.schedule = '请选择有效的执行日期'
    if (schedule.kind === 'weekly' && (!schedule.weekdays.length
      || schedule.weekdays.some((day) => !Number.isInteger(day) || day < 0 || day > 6))) {
      errors.schedule = '请至少选择一个执行日'
    }
    if (schedule.kind === 'monthly' && (!Number.isInteger(schedule.day) || schedule.day < 1 || schedule.day > 31)) {
      errors.schedule = '请选择每月 1–31 日'
    }
  }
  if ((draft.startsOn && !isCalendarDate(draft.startsOn)) || (draft.endsOn && !isCalendarDate(draft.endsOn))) {
    errors.validity = '请选择有效的起止日期'
  } else if (draft.startsOn && draft.endsOn && draft.startsOn > draft.endsOn) {
    errors.validity = '结束日期不能早于开始日期'
  } else if (schedule.kind === 'once' && isCalendarDate(schedule.date)
    && ((draft.startsOn && schedule.date < draft.startsOn) || (draft.endsOn && schedule.date > draft.endsOn))) {
    errors.validity = '执行日期必须在有效期内'
  }
  return errors
}

export function emptyDraft(): AutomationDraft {
  return {
    name: '', prompt: '', schedule: { kind: 'daily', time: '09:00' },
    startsOn: '', endsOn: '', modelId: '', accessMode: 'full', attachments: [], files: [],
  }
}


export const todayInBeijing = () => beijingDate(Date.now())
export const dateBoundary = (date: string) => `${date}T00:00:00+08:00`
export const isActiveRun = (status: RunStatus) => ['queued', 'running', 'interrupted', 'cancel_requested'].includes(status)
export const presentRun = (run: AutomationRunRecord): AutomationRun => ({
  ...run, name: run.name ?? run.taskId ?? run.id,
  date: beijingDate(Date.parse(run.queuedAt)), time: beijingTime(Date.parse(run.queuedAt)),
  durationSeconds: run.startedAt && run.finishedAt ? Math.max(0, Math.round((Date.parse(run.finishedAt) - Date.parse(run.startedAt)) / 1000)) : 0,
})
