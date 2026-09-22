import { describe, expect, it } from 'vitest'

import {
  applyAtomicPlanDeletion,
  cancelComposerSuggestion,
  detectLeadingSlashToken,
  enabledSuggestionIds,
  filterComposerSuggestionGroups,
  isAllowedComposerDraft,
  isSubmittableComposerDraft,
  planClaimParts,
  replaceSlashTokenWithPlan,
} from './composerSuggestions'

describe('composerSuggestions', () => {
  it('detects only a leading slash token at the caret', () => {
    expect(detectLeadingSlashToken('/', 1)).toEqual({ start: 0, end: 1, query: '' })
    expect(detectLeadingSlashToken('  /pl', 5)).toEqual({ start: 2, end: 5, query: 'pl' })
    expect(detectLeadingSlashToken('正文 /pl', 6)).toBeNull()
    expect(detectLeadingSlashToken('/plan 后续', 6)).toBeNull()
  })

  it('分组包含四条指令，Plan 与模型选择可用且支持筛选', () => {
    const all = filterComposerSuggestionGroups('')
    expect(all.map((group) => group.label)).toEqual(['指令', '技能'])
    expect(all[0]?.items).toHaveLength(4)
    expect(all[0]?.items.some(item => item.id === 'permission')).toBe(false)
    expect(enabledSuggestionIds(all)).toEqual(['command-compact', 'command-plan', 'command-model'])
    expect(filterComposerSuggestionGroups('pla')[0]?.items.map((item) => item.id)).toEqual(['plan'])
  })

  it('replaces the live token and derives the decorated Plan claim', () => {
    const hit = detectLeadingSlashToken('  /pl', 5)
    if (!hit) throw new Error('missing slash hit')
    const replaced = replaceSlashTokenWithPlan('  /pl', hit)
    expect(replaced).toEqual({ value: '  /plan ', caret: 8 })
    expect(planClaimParts(replaced.value)).toEqual({
      leading: '  ',
      token: '/plan ',
      content: '',
    })
    expect(planClaimParts('/planner')).toBeNull()
  })

  it('accepts only enabled command prefixes and complete enabled commands', () => {
    for (const value of ['普通消息', '/compact', '/compact ', '/compact 说明', '/', '/p', '/pl', '/pla', '/plan', '/plan ', '/plan 制定方案', '/m', '/mo', '/model']) {
      expect(isAllowedComposerDraft(value), value).toBe(true)
    }
    for (const value of ['/x', '/permission', '/planner', '/pla 正文', '/plan/child', '/model ', '/model 正文']) {
      expect(isAllowedComposerDraft(value), value).toBe(false)
    }
    expect(isAllowedComposerDraft('正文 /unknown')).toBe(true)

    for (const value of ['/compact', '/compact ', '/plan 制定方案', '/plan\n制定方案']) {
      expect(isSubmittableComposerDraft(value), value).toBe(true)
    }
    for (const value of ['/', '/p', '/planner', '/model', '/model 正文', '/plan', '/plan ', '  /plan \n\t']) {
      expect(isSubmittableComposerDraft(value), value).toBe(false)
    }
  })

  it('deletes a complete Plan command as one token without leaving partial syntax', () => {
    expect(applyAtomicPlanDeletion('/plan ', 6, 6, 'backward')).toEqual({ value: '', caret: 0 })
    expect(applyAtomicPlanDeletion('/plan', 5, 5, 'backward')).toEqual({ value: '', caret: 0 })
    expect(applyAtomicPlanDeletion('/plan 任务', 0, 0, 'forward')).toEqual({ value: '任务', caret: 0 })
    expect(applyAtomicPlanDeletion('/plan 任务', 0, 7, 'backward')).toEqual({ value: '务', caret: 0 })
    expect(applyAtomicPlanDeletion('/plan 任务', 7, 7, 'backward')).toBeNull()
  })

  it('cancels the slash trigger and menu as one state', () => {
    expect(cancelComposerSuggestion('/')).toEqual({ value: '', caret: 0 })
    expect(cancelComposerSuggestion('/pla')).toEqual({ value: '', caret: 0 })
    expect(cancelComposerSuggestion('  /p')).toEqual({ value: '', caret: 0 })
    expect(cancelComposerSuggestion('普通消息')).toEqual({ value: '普通消息', caret: 4 })
    expect(cancelComposerSuggestion('/已有内容', {
      start: 0,
      end: 1,
      query: '',
    })).toEqual({ value: '已有内容', caret: 0 })
  })
})
