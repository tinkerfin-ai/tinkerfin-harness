import { redo, undo } from '@codemirror/commands'
import { EditorSelection } from '@codemirror/state'
import type { EditorState, Transaction } from '@codemirror/state'
import { forwardRef, useId, useImperativeHandle, useLayoutEffect, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import { changeComposerText, composerSkillReferences } from '../composerDraft'
import { hasLeadingSkillReference, planClaimParts } from '../composerSuggestions'
import { useI18n } from '../../../i18n'

/** 原生输入处理中文组合输入，文档状态统一管理引用位置与撤销历史 */
export const ComposerInput = forwardRef<HTMLTextAreaElement, {
  draft: EditorState
  disabled: boolean
  busy: boolean
  placeholder: string
  menuId?: string
  activeId?: string
  unavailableIds: readonly string[]
  onChange: (transaction: Transaction) => void
  acceptDraft: (draft: EditorState) => boolean
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  onBlur: () => void
  onAddAttachments: (files: readonly File[]) => void
}>(function ComposerInput({ draft, disabled, busy, placeholder, menuId, activeId, unavailableIds, onChange, acceptDraft, onKeyDown, onBlur, onAddAttachments }, ref) {
  const { t } = useI18n()
  const descriptionId = useId()
  const input = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  const beforeEdit = useRef<EditorSelection | null>(null)
  const latest = useRef({ draft, onChange })
  latest.current = { draft, onChange }
  useImperativeHandle(ref, () => input.current!, [])

  function apply(transaction: Transaction) {
    latest.current.draft = transaction.state
    latest.current.onChange(transaction)
  }

  useLayoutEffect(() => {
    const element = input.current
    if (!element || composing.current) return
    const selection = draft.selection.main
    element.setSelectionRange(selection.from, selection.to, selection.anchor > selection.head ? 'backward' : 'forward')
  }, [draft])

  useLayoutEffect(() => {
    const element = input.current
    if (!element) return
    const capture = (event: InputEvent) => {
      if (disabled) return
      if (event.inputType === 'historyUndo' || event.inputType === 'historyRedo') {
        event.preventDefault()
        const command = event.inputType === 'historyUndo' ? undo : redo
        command({ state: latest.current.draft, dispatch: apply })
        return
      }
      beforeEdit.current = EditorSelection.single(
        element.selectionDirection === 'backward' ? element.selectionEnd : element.selectionStart,
        element.selectionDirection === 'backward' ? element.selectionStart : element.selectionEnd,
      )
    }
    element.addEventListener('beforeinput', capture)
    return () => element.removeEventListener('beforeinput', capture)
  }, [disabled])

  const text = draft.doc.toString()
  const plan = hasLeadingSkillReference(text, draft.field(composerSkillReferences)) ? null : planClaimParts(text)
  const ranges = [
    ...draft.field(composerSkillReferences).map(reference => ({ ...reference, kind: 'skill' as const })),
    ...(plan ? [{ from: plan.leading.length, to: plan.leading.length + plan.token.length, kind: 'plan' as const }] : []),
  ].sort((a, b) => a.from - b.from)
  let position = 0
  const highlighted = ranges.flatMap(range => {
    const before = text.slice(position, range.from)
    position = range.to
    const unavailable = range.kind === 'skill' && unavailableIds.includes(range.skill.id)
    return [before, <mark key={`${range.kind}-${range.from}`} className={range.kind === 'skill' ? 'composer-skill-reference' : 'composer-plan-reference'} data-unavailable={unavailable || undefined}>
      {text.slice(range.from, range.to)}
    </mark>]
  })

  return <div className="composer-input-scroll">
    <div className="composer-input-grow">
      <div className={`composer-input-backdrop${disabled ? ' is-disabled' : ''}`} aria-hidden="true">
        {highlighted}{text.slice(position)}{plan && !plan.content && <span>{t('描述你的任务以生成计划')}</span>}
      </div>
      <span id={descriptionId} className="visually-hidden">{t('技能引用可整段删除，撤销可恢复')}</span>
      <textarea ref={input} className="composer-input" aria-label={t('消息输入')} aria-busy={busy}
        aria-controls={menuId} aria-activedescendant={activeId} aria-autocomplete="list" disabled={disabled}
        aria-describedby={ranges.some(range => range.kind === 'skill') ? descriptionId : undefined}
        value={text} rows={1} placeholder={placeholder}
        onCompositionStart={() => { composing.current = true }}
        onCompositionEnd={() => { composing.current = false }}
        onPaste={event => { const files = [...event.clipboardData.files]; if (files.length) { event.preventDefault(); onAddAttachments(files) } }}
        onDragOver={event => event.preventDefault()}
        onDrop={event => { event.preventDefault(); onAddAttachments([...event.dataTransfer.files]) }}
        onChange={event => {
          const element = event.currentTarget
          const current = latest.current.draft
          const selection = beforeEdit.current
          beforeEdit.current = null
          const state = selection ? current.update({ selection }).state : current
          const transaction = changeComposerText(state, element.value, element.selectionStart, composing.current ? 'input.type.compose' : 'input.type', selection?.main)
          if (!composing.current && !acceptDraft(transaction.state)) {
            element.value = current.doc.toString()
            element.setSelectionRange(current.selection.main.from, current.selection.main.to)
            return
          }
          apply(transaction)
        }}
        onSelect={event => {
          if (composing.current) return
          const element = event.currentTarget
          const current = latest.current.draft
          if (element.value !== current.doc.toString()) return
          const selection = EditorSelection.single(
            element.selectionDirection === 'backward' ? element.selectionEnd : element.selectionStart,
            element.selectionDirection === 'backward' ? element.selectionStart : element.selectionEnd,
          )
          if (!current.selection.eq(selection)) apply(current.update({ selection }))
        }}
        onKeyDown={event => {
          if (!event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229 && !disabled) {
            if ((event.metaKey || event.ctrlKey) && !event.altKey && (event.key.toLowerCase() === 'z' || (event.ctrlKey && event.key.toLowerCase() === 'y'))) {
              event.preventDefault()
              const command = event.shiftKey || event.key.toLowerCase() === 'y' ? redo : undo
              command({ state: latest.current.draft, dispatch: apply })
              return
            }
            if (!event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey && event.currentTarget.selectionStart === event.currentTarget.selectionEnd) {
              const caret = event.currentTarget.selectionStart
              const reference = latest.current.draft.field(composerSkillReferences).find(item => event.key === 'ArrowLeft' ? item.from < caret && caret <= item.to : event.key === 'ArrowRight' && item.from <= caret && caret < item.to)
              if (reference) {
                event.preventDefault()
                apply(latest.current.draft.update({ selection: { anchor: event.key === 'ArrowLeft' ? reference.from : reference.to } }))
                return
              }
            }
          }
          onKeyDown(event)
        }} onBlur={onBlur}
      />
      <div className="composer-input-mirror" aria-hidden="true">{`${text}\n`}</div>
    </div>
  </div>
})
