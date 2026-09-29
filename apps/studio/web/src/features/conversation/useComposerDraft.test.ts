import { act, renderHook } from '@testing-library/react'
import { expect, it } from 'vitest'
import { changeComposerText } from './composerDraft'
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
