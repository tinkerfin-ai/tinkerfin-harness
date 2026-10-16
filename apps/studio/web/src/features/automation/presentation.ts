import type { LocaleContextValue } from '../../i18n/LocaleContext'
import type { AutomationSchedule } from './model'

type Translate = LocaleContextValue['t']

export function formatDate(date: string, locale: string, weekday = false): string {
  return new Intl.DateTimeFormat(locale, weekday ? { weekday: 'short', timeZone: 'UTC' }
    : { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(`${date}T00:00:00Z`))
}

export function formatDateRange(start: string, end: string, locale: string): string {
  const first = new Date(`${start}T00:00:00Z`)
  const last = new Date(`${end}T00:00:00Z`)
  const sameYear = start.slice(0, 4) === end.slice(0, 4)
  const formatter = new Intl.DateTimeFormat(locale, {
    month: 'short', day: 'numeric', timeZone: 'UTC',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
  return `${formatter.format(first)} – ${formatter.format(last)}`
}

export function formatSchedule(schedule: AutomationSchedule, locale: string, t: Translate): string {
  switch (schedule.kind) {
    case 'once': return t('单次 {date} {time}', { date: formatDate(schedule.date, locale), time: schedule.time })
    case 'daily': return t('每天 {time}', { time: schedule.time })
    case 'workdays': return t('工作日 {time}', { time: schedule.time })
    case 'weekly': return t('每{days} {time}', {
      days: [...schedule.weekdays].sort().map((day) => formatDate(`2026-09-${String(7 + day).padStart(2, '0')}`, locale, true)).join(locale === 'zh-CN' ? '、' : ', '),
      time: schedule.time,
    })
    case 'monthly': return t('每月 {day} 日 {time}', { day: schedule.day, time: schedule.time })
    case 'interval': return t('每隔 {count} {unit}', { count: schedule.every, unit: t(({ minutes: '分钟', hours: '小时', days: '天' } as const)[schedule.unit]) })
  }
}
