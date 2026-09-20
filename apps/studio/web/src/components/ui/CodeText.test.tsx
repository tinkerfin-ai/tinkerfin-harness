import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CodeText } from './CodeText'
import { codeLanguage } from './codeLanguages'
import { highlightCode } from './highlightCode'
import type { TokenStream } from 'prismjs'

function sourceOf(tokens: TokenStream): string {
  if (typeof tokens === 'string') return tokens
  if (Array.isArray(tokens)) return tokens.map(sourceOf).join('')
  return sourceOf(tokens.content)
}

describe('只读源码高亮', () => {
  it.each([
    ['json', '{"text":"中文 <script>","count":42,"enabled":true}'],
    ['mermaid', 'flowchart LR\n  A[输入] --> B[输出]\n'],
    ['ts', 'const value: number = 42\n'],
    ['tsx', 'const element = <p>{value}</p>'],
    ['py', 'def answer():\n    return "中文"\n'],
    ['sql', 'SELECT name FROM accounts WHERE active = true;'],
    ['sh', 'echo "$HOME"\n'],
  ])('%s 的着色片段完整保留空白、标点和原文', (language, source) => {
    const tokens = highlightCode(source, codeLanguage(language)!)
    expect(tokens.some(token => typeof token !== 'string')).toBe(true)
    expect(sourceOf(tokens)).toBe(source)
  })

  it('未知语言保持纯文本，HTML 不成为页面元素', () => {
    const source = '<script>alert(1)</script>\n  原文'
    const { container } = render(<pre><CodeText language="unknown">{source}</CodeText></pre>)
    expect(container.textContent).toBe(source)
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelectorAll('span')).toHaveLength(0)
  })

  it('流式正文完成后仍保持同一份源码', async () => {
    const source = '{"count":42}'
    const { container, rerender } = render(<CodeText language="json" isStreaming>{source}</CodeText>)
    expect(container.textContent).toBe(source)
    rerender(<CodeText language="json">{source}</CodeText>)
    expect(await screen.findByText('42')).toBeInTheDocument()
    expect(container.textContent).toBe(source)
  })
})
