/** 会话宽度以 CSS 像素保存，空值表示随可用空间自动调整 */
export type ConversationWidthPreference = number | null

export const CONVERSATION_WIDTH_KEY = 'tinkerfin:conversation-width'
export const MIN_CONVERSATION_WIDTH = 640
export const COMPOSER_WIDTH_EXTRA = 32

export function parseWidthPreference(raw: string | null): ConversationWidthPreference {
  if (raw === null) return null
  const value = Number(raw)
  return Number.isFinite(value) && value >= MIN_CONVERSATION_WIDTH ? value : null
}

export function readWidthPreference(): ConversationWidthPreference {
  try {
    return parseWidthPreference(localStorage.getItem(CONVERSATION_WIDTH_KEY))
  } catch {
    // 本地存储不可用时，仍允许在当前页面调整
    return null
  }
}

export function persistWidthPreference(value: number): void {
  try {
    localStorage.setItem(CONVERSATION_WIDTH_KEY, String(value))
  } catch {
    // 存储失败不影响当前页面已选择的宽度
  }
}

export function resolveConversationWidth(column: number, gutter: number, preference: ConversationWidthPreference) {
  const max = Math.max(0, Math.floor(column - 2 * gutter - COMPOSER_WIDTH_EXTRA))
  const desired = preference === null ? Math.max(680, Math.min(column * 0.64, 920)) : Math.max(MIN_CONVERSATION_WIDTH, preference)
  return { width: Math.min(max, Math.round(desired)), max }
}
