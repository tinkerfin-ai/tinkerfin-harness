import { act, renderHook } from '@testing-library/react'
import { undo } from '@codemirror/commands'
import { expect, it } from 'vitest'
import { changeComposerText, createComposerDraft, insertComposerSkill } from './composerDraft'
import { useComposerDraft } from './useComposerDraft'

it('发送清空后到达的旧选择事件不能恢复已提交正文', () => {
  const { result } = renderHook(() => useComposerDraft('/plan 制定执行方案'))
  const selection = result.current.state.update({ selection: { anchor: 0 } })
  act(() => {
    result.current.setText('')
    result.current.apply(selection)
  })
  expect(result.current.text).toBe('')
  act(() => result.current.apply(changeComposerText(result.current.state, '下一条消息')))
  expect(result.current.text).toBe('下一条消息')
})

it.each([true, false])('切换工作区后按会话身份恢复技能引用和撤销历史：当前选中=%s', selected => {
  const skill = { id: 'report', name: 'report', description: '解读报告' }
  const original = createComposerDraft('待完善的分析')
  const edited = insertComposerSkill(original, skill).state
  const store = new Map([['thread:thread', edited], ['thread:other', createComposerDraft('其他会话草稿')]])
  const current = selected ? 'thread:thread' : 'thread:other'
  const view = renderHook(({ key }) => useComposerDraft('', { key, store }), { initialProps: { key: current } })
  view.rerender({ key: 'first:' })
  view.unmount()
  const moved = renderHook(() => useComposerDraft('', { key: 'thread:thread', store }))
  expect(moved.result.current.text).toBe(edited.doc.toString())
  expect(moved.result.current.references.map(reference => reference.skill.id)).toEqual(['report'])
  act(() => { expect(undo({ state: moved.result.current.state, dispatch: moved.result.current.apply })).toBe(true) })
  expect(moved.result.current.text).toBe(original.doc.toString())
  expect(moved.result.current.references).toEqual([])
  expect(store.get('thread:other')?.doc.toString()).toBe('其他会话草稿')
  moved.unmount()
})

it('待登记草稿转为正式会话身份后继续保留引用和编辑状态', () => {
  const state = insertComposerSkill(createComposerDraft(), { id: 'report', name: 'report', description: '报告' }).state
  const store = new Map([['project:pending:run', state]])
  const view = renderHook(({ key }) => useComposerDraft('', { key, store }), { initialProps: { key: 'project:pending:run' } })
  act(() => view.result.current.moveTo('thread:registered'))
  view.rerender({ key: 'thread:registered' })
  expect(view.result.current.state).toBe(state)
  expect(view.result.current.references[0].skill.id).toBe('report')
  expect(store.has('project:pending:run')).toBe(false)
  view.unmount()
})
