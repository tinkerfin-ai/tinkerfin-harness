import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import type { SetStateAction } from 'react'
import type { EditorState, Transaction } from '@codemirror/state'
import { composerSkillReferences, createComposerDraft } from './composerDraft'

/** 原文、引用及撤销历史共同保存；失败恢复不会拆散技能与文字 */
export function useComposerDraft(initialText = '', retention?: { key: string; store: Map<string, EditorState> }) {
  const [state, setState] = useState(() => retention?.store.get(retention.key) ?? createComposerDraft(initialText))
  const current = useRef(state)
  const owner = useRef(retention)
  const revision = useRef(0)
  useLayoutEffect(() => {
    if (owner.current?.key === retention?.key && owner.current?.store === retention?.store) return
    if (owner.current) owner.current.store.set(owner.current.key, current.current)
    owner.current = retention
    revision.current += 1
    current.current = retention?.store.get(retention.key) ?? createComposerDraft(initialText)
    setState(current.current)
  }, [retention, initialText])
  const restore = useCallback((next: EditorState) => {
    revision.current += 1
    current.current = next
    if (owner.current) owner.current.store.set(owner.current.key, next)
    setState(next)
  }, [])
  const apply = useCallback((transaction: Transaction) => {
    // 清空、恢复或切换草稿后，旧输入事件不能覆盖当前内容
    if (transaction.startState !== current.current) return
    if (transaction.docChanged || transaction.startState.field(composerSkillReferences) !== transaction.state.field(composerSkillReferences)) revision.current += 1
    current.current = transaction.state
    if (owner.current) owner.current.store.set(owner.current.key, transaction.state)
    setState(transaction.state)
  }, [])
  const setText = useCallback((value: SetStateAction<string>) => {
    restore(createComposerDraft(typeof value === 'function' ? value(current.current.doc.toString()) : value))
  }, [restore])
  const moveTo = useCallback((key: string) => {
    if (!owner.current || owner.current.key === key) return
    const { store, key: previous } = owner.current
    store.set(key, current.current); store.delete(previous)
    owner.current = { store, key }
  }, [])
  return { state, revision, apply, restore, setText, moveTo, text: state.doc.toString(), references: state.field(composerSkillReferences) }
}
