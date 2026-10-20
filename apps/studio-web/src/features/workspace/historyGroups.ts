import type { Conversation } from '../../types'

const MILLISECONDS_PER_DAY = 86_400_000
const TIMEZONE_SUFFIX = /(?:[zZ]|[+-]\d{2}:\d{2})$/

export interface ConversationHistoryGroup {
  key: string
  label: string
  items: Conversation[]
}

const parseHistoryTimestamp = (value: string) => new Date(
  TIMEZONE_SUFFIX.test(value) ? value : `${value}Z`,
)

const localDayOrdinal = (value: Date) => Date.UTC(
  value.getFullYear(),
  value.getMonth(),
  value.getDate(),
) / MILLISECONDS_PER_DAY

const compareUpdatedAtDescending = (left: Conversation, right: Conversation) => (
  parseHistoryTimestamp(right.updatedAt).getTime()
  - parseHistoryTimestamp(left.updatedAt).getTime()
)

export const groupConversationHistory = (
  conversations: Conversation[],
  dayRanges: number[],
  now = new Date(),
  locale: 'zh-CN' | 'en' = 'zh-CN',
): ConversationHistoryGroup[] => {
  const orderedRanges = [...new Set(dayRanges)]
    .filter((value) => Number.isInteger(value) && value > 0)
    .sort((left, right) => left - right)
  const pinned = conversations
    .filter((conversation) => conversation.pinned)
    .sort(compareUpdatedAtDescending)
  const recent = conversations
    .filter((conversation) => !conversation.pinned)
    .sort(compareUpdatedAtDescending)
  const nowOrdinal = localDayOrdinal(now)
  const grouped = new Map<string, ConversationHistoryGroup>()

  if (pinned.length > 0) {
    grouped.set('pinned', { key: 'pinned', label: locale === 'en' ? 'Pinned' : '置顶', items: pinned })
  }

  for (const conversation of recent) {
    const updatedAt = parseHistoryTimestamp(conversation.updatedAt)
    const ageInDays = Number.isNaN(updatedAt.getTime())
      ? Number.POSITIVE_INFINITY
      : Math.max(0, nowOrdinal - localDayOrdinal(updatedAt))
    const range = orderedRanges.find((value) => ageInDays <= value)
    const key = ageInDays === 0
      ? 'today'
      : range == null
        ? 'earlier'
        : `days-${range}`
    const label = ageInDays === 0
      ? (locale === 'en' ? 'Today' : '今天')
      : range == null
        ? (locale === 'en' ? 'Earlier' : '更早')
        : locale === 'en' ? `Within ${range} days` : `${range}天内`
    const existing = grouped.get(key)
    if (existing) existing.items.push(conversation)
    else grouped.set(key, { key, label, items: [conversation] })
  }

  return [...grouped.values()]
}
