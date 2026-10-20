import { useEffect, useRef } from 'react'
import { Annotation, Compartment, EditorState } from '@codemirror/state'
import { EditorView, highlightActiveLine, keymap, lineNumbers } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { json } from '@codemirror/lang-json'
import { HighlightStyle, bracketMatching, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { lintGutter, setDiagnostics } from '@codemirror/lint'
import { useI18n } from '../../i18n'
import type { OptionsIssue } from './modelOptions'

const externalUpdate = Annotation.define<boolean>()

export interface ModelOptionsEditorProps {
  value: string
  onChange: (value: string) => void
  issues: OptionsIssue[]
  issueMessage: (issue: OptionsIssue) => string
  disabled?: boolean
  descriptionId: string
}

/** 编辑 JSON 参数并标出校验错误 */
export default function ModelOptionsEditor({ value, onChange, issues, issueMessage, disabled = false, descriptionId }: ModelOptionsEditorProps) {
  const { t } = useI18n()
  const container = useRef<HTMLDivElement>(null)
  const editor = useRef<EditorView | null>(null)
  const initialValue = useRef(value)
  const change = useRef(onChange)
  const options = useRef(new Compartment())
  useEffect(() => { change.current = onChange }, [onChange])
  useEffect(() => {
    if (!container.current) return
    const view = new EditorView({
      parent: container.current,
      state: EditorState.create({
        doc: initialValue.current,
        extensions: [
          json(), lineNumbers(), history(), bracketMatching(), highlightActiveLine(), lintGutter(),
          EditorView.theme({
            '&': { fontSize: 'var(--type-meta-size)', backgroundColor: 'var(--color-layer-1)', color: 'var(--color-text-primary)' },
            '&.cm-focused': { outline: 'none', backgroundColor: 'var(--color-layer-2)' },
            '.cm-scroller': { fontFamily: 'var(--font-code)', fontWeight: 'var(--weight-regular)', lineHeight: 'var(--type-meta-line)', maxHeight: 'calc(var(--space-12) * 5)', overflow: 'auto' },
            '.cm-content': { minHeight: 'calc(var(--space-12) * 3)', paddingBlock: 'var(--space-2)', caretColor: 'var(--color-text-primary)' },
            '.cm-gutters': { backgroundColor: 'var(--color-layer-2)', color: 'var(--color-text-secondary)', borderColor: 'var(--color-border-subtle)' },
            '.cm-activeLine, .cm-activeLineGutter': { backgroundColor: 'var(--color-hover)' },
            '.cm-cursor': { borderLeftColor: 'var(--color-text-primary)' },
            '.cm-tooltip': { fontFamily: 'var(--font-ui)', fontWeight: 'var(--weight-ui)', backgroundColor: 'var(--color-layer-1)', color: 'var(--color-text-primary)', borderColor: 'var(--color-border)', borderRadius: 'var(--radius-md)' },
            '.cm-diagnosticText': { color: 'var(--color-text-primary)' },
            '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': { backgroundColor: 'var(--color-active)' },
          }),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          options.current.of([]),
          syntaxHighlighting(HighlightStyle.define([
            { tag: tags.propertyName, color: 'var(--color-text-primary)' },
            { tag: tags.string, color: 'var(--color-success-text)' },
            { tag: [tags.number, tags.bool, tags.null], color: 'var(--color-brand-text)' },
            { tag: tags.punctuation, color: 'var(--color-text-secondary)' },
          ])),
          EditorView.updateListener.of((update) => {
            if (update.docChanged && !update.transactions.some(transaction => transaction.annotation(externalUpdate)))
              change.current(update.state.doc.toString())
          }),
        ],
      }),
    })
    editor.current = view
    return () => { editor.current = null; view.destroy() }
  }, [])
  useEffect(() => {
    const view = editor.current
    if (view && view.state.doc.toString() !== value)
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value }, annotations: externalUpdate.of(true) })
  }, [value])
  useEffect(() => {
    const view = editor.current
    if (!view) return
    view.dispatch({ effects: options.current.reconfigure([
      EditorState.readOnly.of(disabled), EditorView.editable.of(!disabled),
      EditorView.contentAttributes.of({ 'aria-label': t('高级参数 JSON'), 'aria-invalid': String(issues.length > 0), 'aria-describedby': descriptionId, role: 'textbox', 'aria-multiline': 'true' }),
    ]) })
    view.dispatch(setDiagnostics(view.state, issues.map((issue) => ({ from: Math.min(issue.from, view.state.doc.length), to: Math.min(issue.to, view.state.doc.length), severity: 'error', message: issueMessage(issue) }))))
  }, [disabled, issues, issueMessage, descriptionId, t])
  return <div className="model-options-editor" ref={container} />
}
