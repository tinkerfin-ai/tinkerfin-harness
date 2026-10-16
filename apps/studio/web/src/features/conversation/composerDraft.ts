import { history, invertedEffects, isolateHistory } from '@codemirror/commands'
import { EditorState, StateEffect, StateField, Transaction } from '@codemirror/state'
import type { ChangeDesc, ChangeSpec } from '@codemirror/state'
import type { ComposerSkill, SlashTokenHit } from './composerSuggestions'
import { hasLeadingSkillReference } from './composerSuggestions'

export interface ComposerSkillReference {
  from: number
  to: number
  skill: ComposerSkill
}

function mapReferences(references: readonly ComposerSkillReference[], changes: ChangeDesc) {
  return references.flatMap(reference => {
    let touched = false
    changes.iterChangedRanges((from, to) => {
      if (from < reference.to && to > reference.from) touched = true
    })
    if (touched) return []
    return [{ ...reference, from: changes.mapPos(reference.from, 1), to: changes.mapPos(reference.to, -1) }]
  })
}

const restoreReferences = StateEffect.define<readonly ComposerSkillReference[]>({ map: mapReferences })

/** 引用的身份与文本位置共同进入撤销记录，周围的空白和中文不参与识别 */
export const composerSkillReferences = StateField.define<readonly ComposerSkillReference[]>({
  create: () => [],
  update(references, transaction) {
    let next = transaction.docChanged ? mapReferences(references, transaction.changes) : references
    for (const effect of transaction.effects) if (effect.is(restoreReferences)) next = effect.value
    return next
  },
})

export function composerAtomicRanges(state: EditorState) {
  const ranges = state.field(composerSkillReferences).map(({ from, to }) => ({ from, to }))
  const plan = /^(\s*)\/plan(?: |$)/.exec(state.doc.toString())
  if (plan && !hasLeadingSkillReference(state.doc.toString(), state.field(composerSkillReferences))) ranges.unshift({ from: 0, to: plan[0].length })
  return ranges
}

function expandRange(state: EditorState, from: number, to: number) {
  for (const range of composerAtomicRanges(state)) {
    if (from < range.to && to > range.from) {
      from = Math.min(from, range.from)
      to = Math.max(to, range.to)
    }
  }
  return { from, to }
}

const atomicEdits = EditorState.transactionFilter.of(transaction => {
  if (!transaction.docChanged || transaction.isUserEvent('undo') || transaction.isUserEvent('redo')) return transaction
  let expanded = false
  const edits: Array<{ from: number; to: number; insert: string }> = []
  transaction.changes.iterChanges((from, to, _nextFrom, _nextTo, insert) => {
    const range = expandRange(transaction.startState, from, to)
    expanded ||= range.from !== from || range.to !== to
    const previous = edits.at(-1)
    if (previous && previous.to > range.from) {
      previous.to = Math.max(previous.to, range.to)
      previous.insert += insert.toString()
    } else edits.push({ ...range, insert: insert.toString() })
  })
  if (!expanded) return transaction
  const changes = transaction.startState.changes(edits)
  return {
    changes,
    selection: { anchor: changes.mapPos(transaction.startState.selection.main.head, 1) },
    annotations: Transaction.userEvent.of(transaction.annotation(Transaction.userEvent) ?? 'input'),
  }
})

export function createComposerDraft(text = '') {
  return EditorState.create({
    doc: text,
    selection: { anchor: text.length },
    extensions: [
      composerSkillReferences,
      atomicEdits,
      history(),
      invertedEffects.of(transaction => transaction.startState.field(composerSkillReferences) === transaction.state.field(composerSkillReferences)
        ? [] : [restoreReferences.of(transaction.startState.field(composerSkillReferences))]),
    ],
  })
}

/** 已有选区按普通输入替换；空草稿使用产品默认短句 */
export function insertComposerSkill(state: EditorState, skill: ComposerSkill, hit: SlashTokenHit | null = null, emptyPrompt?: string) {
  const references = state.field(composerSkillReferences)
  if (references.length >= 8 || references.some(reference => reference.skill.id === skill.id)) return state.update({})
  const text = state.doc.toString()
  const selection = state.selection.main
  const requested = hit ? { from: hit.start, to: hit.end } : { from: selection.from, to: selection.to }
  const empty = !(text.slice(0, requested.from) + text.slice(requested.to)).trim()
  const range = empty ? { from: 0, to: text.length } : expandRange(state, requested.from, requested.to)
  const label = `/${skill.name}`
  const insert = empty ? emptyPrompt ?? `用 ${label} 技能帮我` : label
  const changes = state.changes({ ...range, insert })
  const from = range.from + (empty ? insert.indexOf(label) : 0)
  return state.update({
    changes,
    selection: { anchor: range.from + insert.length },
    effects: restoreReferences.of([
      ...mapReferences(references, changes), { from, to: from + label.length, skill },
    ].sort((a, b) => a.from - b.from)),
    annotations: [Transaction.userEvent.of('input.skill'), isolateHistory.of('full')],
  })
}

/** 根据一次编辑前的选区定位改动，重复文本也不会让引用身份跳到别处 */
export function changeComposerText(state: EditorState, text: string, caret = text.length, inputType = 'input', editRange?: { from: number; to: number }) {
  const previous = state.doc.toString()
  // beforeinput 提供精确旧选区；未携带该信息的输入以新光标定位最小改动
  const start = editRange?.from ?? Math.max(0, caret - Math.max(0, text.length - previous.length))
  let from = 0
  const prefixLimit = Math.min(start, previous.length, text.length)
  while (from < prefixLimit && previous[from] === text[from]) from += 1
  let to = previous.length
  let end = text.length
  while (to > (editRange?.to ?? from) && end > from && previous[to - 1] === text[end - 1]) { to -= 1; end -= 1 }
  const changes: ChangeSpec = { from, to, insert: text.slice(from, end) }
  return state.update({ changes, selection: { anchor: caret }, annotations: Transaction.userEvent.of(inputType) })
}

export function deleteComposerAtom(state: EditorState, start: number, end: number, direction: 'backward' | 'forward') {
  const from = start === end && direction === 'backward' ? Math.max(0, start - 1) : start
  const to = start === end && direction === 'forward' ? Math.min(state.doc.length, end + 1) : end
  if (!composerAtomicRanges(state).some(range => from < range.to && to > range.from)) return null
  const range = expandRange(state, from, to)
  return state.update({ changes: range, selection: { anchor: range.from }, annotations: [Transaction.userEvent.of('delete'), isolateHistory.of('full')] })
}
