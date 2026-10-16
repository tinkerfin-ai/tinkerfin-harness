import { memo, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { Token, TokenStream } from 'prismjs'
import type { highlightCode } from './highlightCode'
import { codeLanguage } from './codeLanguages'
import './codeText.css'

let highlighter: Promise<typeof highlightCode> | undefined
function loadHighlighter() {
  highlighter ??= import('./highlightCode').then(module => module.highlightCode).catch(error => {
    highlighter = undefined
    throw error
  })
  return highlighter
}

function tokenContent(value: TokenStream): ReactNode {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map((token, index) => (
    typeof token === 'string' ? token : tokenElement(token, index)
  ))
  return tokenElement(value, 0)
}

function tokenElement(token: Token, key: number): ReactNode {
  const kinds = [token.type, ...[token.alias ?? []].flat()]
  return <span key={key} className={kinds.map(kind => `ui-code-token--${kind}`).join(' ')}>{tokenContent(token.content)}</span>
}

/** 只读代码共享高亮；未知语言、大文本和生成中的内容仍完整显示 */
export const CodeText = memo(function CodeText({
  children, language, isStreaming = false,
}: { children: string; language?: string; isStreaming?: boolean }) {
  const name = codeLanguage(language)
  const enabled = Boolean(name && !isStreaming && children.length <= 100_000)
  const [tokenize, setTokenize] = useState<typeof highlightCode>()
  useEffect(() => {
    if (!enabled) return
    let active = true
    void loadHighlighter().then(value => {
      if (active) setTokenize(() => value)
    }).catch(() => { /* 高亮资源不可用时保留可读、可复制的原文 */ })
    return () => { active = false }
  }, [enabled])
  const content = useMemo(() => {
    if (!enabled || !name || !tokenize) return children
    return tokenContent(tokenize(children, name))
  }, [children, enabled, name, tokenize])
  return <code className="ui-code-text" data-language={name ?? language}>{content}</code>
})
