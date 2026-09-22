export interface ComposerSuggestionItem {
  id: string
  name: string
  description: string
  disabled: boolean
}

export interface ComposerSuggestionGroup {
  id: 'command' | 'skill'
  label: string
  items: readonly ComposerSuggestionItem[]
}

export interface SlashTokenHit {
  start: number
  end: number
  query: string
}

export interface ComposerDraftEdit {
  value: string
  caret: number
}

const COMMAND_ITEMS: readonly ComposerSuggestionItem[] = [
  { id: 'compact', name: 'compact', description: '压缩较早的会话历史', disabled: false },
  { id: 'goal', name: 'goal', description: '设置或查看长期任务目标', disabled: true },
  { id: 'plan', name: 'plan', description: '进入 Plan 模式', disabled: false },
  { id: 'model', name: 'model', description: '选择本会话使用的模型', disabled: false },
]

const SKILL_ITEMS: readonly ComposerSuggestionItem[] = [
  {
    id: 'skills-unavailable',
    name: '技能暂未开放',
    description: '当前版本暂不支持从输入框调用技能',
    disabled: true,
  },
]

export const COMPOSER_SUGGESTION_GROUPS: readonly ComposerSuggestionGroup[] = [
  { id: 'command', label: '指令', items: COMMAND_ITEMS },
  { id: 'skill', label: '技能', items: SKILL_ITEMS },
]

const ENABLED_SLASH_NAMES = COMPOSER_SUGGESTION_GROUPS.flatMap((group) => group.items)
  .filter((item) => !item.disabled)
  .map((item) => item.name)

const leadingSlashCommand = (value: string) => {
  const match = /^(\s*)\/(\S*)/.exec(value)
  if (!match) return null
  const leading = match[1] ?? ''
  const query = match[2] ?? ''
  const tokenEnd = leading.length + 1 + query.length
  return { query, remainder: value.slice(tokenEnd) }
}

export const isAllowedComposerDraft = (value: string) => {
  const command = leadingSlashCommand(value)
  if (!command || !command.query) return true
  const matchingNames = ENABLED_SLASH_NAMES.filter((name) => name.startsWith(command.query))
  if (matchingNames.length === 0) return false
  return command.remainder === ''
    || (/^\s/.test(command.remainder) && (command.query === 'plan' || command.query === 'compact'))
}

export const isSubmittableComposerDraft = (value: string) => {
  const command = leadingSlashCommand(value)
  if (!command) return true
  if (command.query === 'compact') return true
  return command.query === 'plan'
    && Boolean(command.remainder.trim())
}

export const cancelComposerSuggestion = (
  value: string,
  hit?: SlashTokenHit,
): ComposerDraftEdit => {
  if (hit) {
    return {
      value: `${value.slice(0, hit.start)}${value.slice(hit.end)}`,
      caret: hit.start,
    }
  }
  const slashToken = /^\s*\/[^\s/]*/.exec(value)?.[0]
  if (!slashToken) return { value, caret: value.length }
  return {
    value: value.slice(slashToken.length),
    caret: 0,
  }
}

const planCommandTokenRange = (value: string) => {
  const match = /^(\s*)\/plan(?: |$)/.exec(value)
  if (!match) return null
  const leadingLength = (match[1] ?? '').length
  return {
    start: 0,
    end: leadingLength + 5 + (value[leadingLength + 5] === ' ' ? 1 : 0),
  }
}

export const applyAtomicPlanDeletion = (
  value: string,
  selectionStart: number,
  selectionEnd: number,
  direction: 'backward' | 'forward',
): ComposerDraftEdit | null => {
  const token = planCommandTokenRange(value)
  if (!token) return null
  const collapsed = selectionStart === selectionEnd
  const intersectsToken = !collapsed
    && selectionStart < token.end
    && selectionEnd > token.start
  const deletesBackwardIntoToken = collapsed
    && direction === 'backward'
    && selectionStart > token.start
    && selectionStart <= token.end
  const deletesForwardIntoToken = collapsed
    && direction === 'forward'
    && selectionStart >= token.start
    && selectionStart < token.end
  if (!intersectsToken && !deletesBackwardIntoToken && !deletesForwardIntoToken) return null

  const editStart = collapsed ? token.start : Math.min(selectionStart, token.start)
  const editEnd = collapsed ? token.end : Math.max(selectionEnd, token.end)
  return {
    value: `${value.slice(0, editStart)}${value.slice(editEnd)}`,
    caret: editStart,
  }
}

export const detectLeadingSlashToken = (
  value: string,
  caret: number,
): SlashTokenHit | null => {
  const prefix = value.slice(0, caret)
  const match = /^(\s*)\/([^\s/]*)$/.exec(prefix)
  if (!match) return null
  const leading = match[1] ?? ''
  const query = match[2] ?? ''
  return { start: leading.length, end: caret, query }
}

export const filterComposerSuggestionGroups = (
  query: string,
): readonly ComposerSuggestionGroup[] => {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return COMPOSER_SUGGESTION_GROUPS
  return COMPOSER_SUGGESTION_GROUPS
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => (
        item.name.toLowerCase().includes(normalized)
        || item.description.toLowerCase().includes(normalized)
      )),
    }))
    .filter((group) => group.items.length > 0)
}

export const enabledSuggestionIds = (
  groups: readonly ComposerSuggestionGroup[],
) => groups.flatMap((group) => group.items
  .filter((item) => !item.disabled)
  .map((item) => `${group.id}-${item.id}`))

export const replaceSlashTokenWithPlan = (
  value: string,
  hit: SlashTokenHit,
) => {
  const replacement = '/plan '
  return {
    value: `${value.slice(0, hit.start)}${replacement}${value.slice(hit.end)}`,
    caret: hit.start + replacement.length,
  }
}

export const planClaimParts = (value: string) => {
  const match = /^(\s*)(\/plan )(.*)$/s.exec(value)
  if (!match) return null
  return {
    leading: match[1] ?? '',
    token: match[2] ?? '',
    content: match[3] ?? '',
  }
}
