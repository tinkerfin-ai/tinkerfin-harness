import { parseComposerSubmission } from './composerCommand'
import { redo, undo } from '@codemirror/commands'
import { describe, expect, it } from 'vitest'
import { changeComposerText, composerSkillReferences, createComposerDraft, deleteComposerAtom, insertComposerSkill } from './composerDraft'

const skill = { id: 'report', name: 'ai-report-interpreter', description: '解读报告' }
const selected = () => insertComposerSkill(createComposerDraft(), skill).state

describe('带技能引用的草稿', () => {
  it('空草稿插入短句，在已有文本选区插入时保留其他文字', () => {
    expect(selected().doc.toString()).toBe('用 /ai-report-interpreter 技能帮我')
    const state = createComposerDraft('请帮我分析').update({ selection: { anchor: 3 } }).state
    const next = insertComposerSkill(state, skill).state
    expect(next.doc.toString()).toBe('请帮我/ai-report-interpreter分析')
    expect(next.field(composerSkillReferences).map(reference => reference.skill.id)).toEqual(['report'])
  })

  it.each(['用/ai-report-interpreter', '/ai-report-interpreter'])('编辑周围文字为 %s 后仍保留同一技能', text => {
    let state = selected()
    const reference = state.field(composerSkillReferences)[0]
    state = state.update({ changes: { from: reference.to, to: state.doc.length } }).state
    state = state.update({ changes: { from: text.startsWith('用') ? 1 : 0, to: 2 } }).state
    expect(state.doc.toString()).toBe(text)
    expect(state.field(composerSkillReferences).map(item => item.skill.id)).toEqual(['report'])
  })

  it.each(['backward', 'forward'] as const)('从 %s 删除引用时整段删除，撤销和重做同时恢复身份', direction => {
    let state = selected()
    const reference = state.field(composerSkillReferences)[0]
    const position = direction === 'backward' ? reference.to : reference.from
    state = deleteComposerAtom(state, position, position, direction)!.state
    expect(state.doc.toString()).toBe('用  技能帮我')
    expect(state.field(composerSkillReferences)).toEqual([])
    expect(undo({ state, dispatch: transaction => { state = transaction.state } })).toBe(true)
    expect(state.doc.toString()).toBe('用 /ai-report-interpreter 技能帮我')
    expect(state.field(composerSkillReferences)[0].skill.id).toBe('report')
    expect(redo({ state, dispatch: transaction => { state = transaction.state } })).toBe(true)
    expect(state.field(composerSkillReferences)).toEqual([])
  })

  it('部分选中引用后粘贴会整体替换，撤销还原原文与引用', () => {
    let state = selected().update({ selection: { anchor: 3, head: 6 } }).state
    const value = state.doc.toString()
    state = changeComposerText(state, value.slice(0, 3) + '新的' + value.slice(6), 5).state
    expect(state.doc.toString()).toBe('用 新的 技能帮我')
    expect(state.field(composerSkillReferences)).toEqual([])
    undo({ state, dispatch: transaction => { state = transaction.state } })
    expect(state.doc.toString()).toBe(value)
    expect(state.field(composerSkillReferences)[0].skill.id).toBe('report')
  })

  it('引用旁的中文组合输入保持引用范围，撤销选择技能时一并删除默认短句', () => {
    let state = selected()
    const text = state.doc.toString()
    state = changeComposerText(state, text + '分', text.length + 1, 'input.type.compose').state
    state = changeComposerText(state, text + '分析', text.length + 2, 'input.type.compose').state
    expect(state.field(composerSkillReferences)[0]).toMatchObject({ from: 2, to: 24, skill })
    let initial = selected()
    undo({ state: initial, dispatch: transaction => { initial = transaction.state } })
    expect(initial.doc.toString()).toBe('')
    expect(initial.field(composerSkillReferences)).toEqual([])
  })

  it('重复的普通文本不改变引用身份，手写同名文本不会自动绑定', () => {
    let state = createComposerDraft('/ai-report-interpreter')
    expect(state.field(composerSkillReferences)).toEqual([])
    state = insertComposerSkill(state, skill).state
    state = state.update({ selection: { anchor: 0, head: 22 } }).state
    state = changeComposerText(state, '/ai-report-interpreter', 0).state
    expect(state.field(composerSkillReferences)[0]).toMatchObject({ from: 0, to: 22, skill })
  })

  it('重复选择不会增加引用，Plan 删除仍是完整指令', () => {
    let state = selected()
    state = insertComposerSkill(state, skill).state
    expect(state.field(composerSkillReferences)).toHaveLength(1)
    const plan = createComposerDraft('/plan 任务')
    expect(deleteComposerAtom(plan, 3, 3, 'backward')!.newDoc.toString()).toBe('任务')
  })
})


it('未携带旧选区的输入依照新光标定位删除，保留旁边的引用', () => {
  let state = selected()
  state = changeComposerText(state, '用 /ai-report-interpreter', 24).state
  state = changeComposerText(state, '用/ai-report-interpreter', 1).state
  state = changeComposerText(state, '/ai-report-interpreter', 0).state
  expect(state.field(composerSkillReferences)[0]).toMatchObject({ from: 0, to: 22, skill })
})


it('英文默认短句的引用位置仍与技能身份绑定', () => {
  const state = insertComposerSkill(createComposerDraft(), skill, null, 'Use /ai-report-interpreter to help me').state
  expect(state.doc.toString()).toBe('Use /ai-report-interpreter to help me')
  expect(state.field(composerSkillReferences)[0]).toMatchObject({ from: 4, to: 26, skill })
})

it('每份草稿最多八个不同技能，超出上限保留原文与已有引用', () => {
  let state = createComposerDraft('分析：')
  for (let index = 0; index < 8; index += 1) state = insertComposerSkill(state, { ...skill, id: String(index), name: `skill-${index}` }).state
  const original = state.doc.toString()
  state = insertComposerSkill(state, { ...skill, id: 'ninth' }).state
  expect(state.doc.toString()).toBe(original)
  expect(state.field(composerSkillReferences)).toHaveLength(8)
})


it.each(['plan', 'compact', 'model'])('技能名为 %s 时仍按引用发送和删除，不解释为命令', name => {
  let state = insertComposerSkill(createComposerDraft(), { ...skill, name }).state
  state = state.update({ changes: { from: 0, to: 2 } }).state
  expect(parseComposerSubmission(state.doc.toString(), state.field(composerSkillReferences))).toEqual({ kind: 'message', content: `/${name} 技能帮我` })
  const reference = state.field(composerSkillReferences)[0]
  expect(deleteComposerAtom(state, reference.to, reference.to, 'backward')!.newDoc.toString()).toBe(' 技能帮我')
})
