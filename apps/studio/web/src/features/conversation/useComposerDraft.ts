import { useCallback, useRef, useState } from 'react'
import type { SetStateAction } from 'react'
import type { EditorState, Transaction } from '@codemirror/state'
import { composerSkillReferences, createComposerDraft } from './composerDraft'

/** 原文、引用及撤销历史共同保存；失败恢复不会拆散技能与文字 */
export function useComposerDraft(initialText = '') {
  const [state, setState] = useState(() => createComposerDraft(initialText))
  const current = useRef(state)
  const revision = useRef(0)
  const restore = useCallback((next: EditorState) => {
    revision.current += 1
    current.current = next
    setState(next)
  }, [])
  const apply = useCallback((transaction: Transaction) => {
    // 清空、恢复或切换草稿后，旧输入事件不能覆盖当前内容
    if (transaction.startState !== current.current) return
    if (transaction.docChanged || transaction.startState.field(composerSkillReferences) !== transaction.state.field(composerSkillReferences)) revision.current += 1
    current.current = transaction.state
    setState(transaction.state)
  }, [])
  const setText = useCallback((value: SetStateAction<string>) => {
    restore(createComposerDraft(typeof value === 'function' ? value(current.current.doc.toString()) : value))
  }, [restore])
  return { state, revision, apply, restore, setText, text: state.doc.toString(), references: state.field(composerSkillReferences) }
}
