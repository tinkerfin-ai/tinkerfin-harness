import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import conversationStyles from '../conversation.css?raw'
import { MarkdownContent } from './MarkdownContent'

describe('MarkdownContent links', () => {
  it('keeps Chinese instructions after a bare URL outside the link', () => {
    const content = '访问 https://www.baidu.com，了解该网站的主营业务。返回不超过50字的中文总结，说明百度是做什么业务的。'
    render(<MarkdownContent content={content} />)

    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://www.baidu.com')
    expect(link).toHaveTextContent('https://www.baidu.com')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveClass('markdown-bare-url')
    expect(link).not.toHaveTextContent('了解该网站的主营业务')
    expect(link.parentElement).toHaveTextContent(content)
  })

  it('preserves explicit Markdown link labels', () => {
    render(<MarkdownContent content="访问 [百度官网](https://www.baidu.com) 和 [https://www.baidu.com中文说明](https://example.com)" />)

    const link = screen.getByRole('link', { name: '百度官网' })
    expect(link).toHaveAttribute('href', 'https://www.baidu.com')
    expect(link).not.toHaveClass('markdown-bare-url')
    expect(screen.getByRole('link', { name: 'https://www.baidu.com中文说明' })).toHaveAttribute(
      'href',
      'https://example.com',
    )
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('keeps relative application links in the current tab', () => {
    render(<MarkdownContent content="[会话帮助](/help/conversations)" />)

    const link = screen.getByRole('link', { name: '会话帮助' })
    expect(link).not.toHaveAttribute('target')
    expect(link).not.toHaveAttribute('rel')
  })
})

const completeFixture = `# 一级标题

第一段正文。

## 二级标题

第二段正文，用于核对连续段落节奏。

### 三级标题

#### 四级标题

##### 五级标题

###### 六级标题

1. 第一项
   - 二级项目
     1. 三级项目

> 一段引用

行内代码 \`const answer = 42\`。

长链接 [TinkerFin 前端规范文档](https://example.com/docs/frontend/visual-system/markdown-contract-and-accessibility-checklist)。

---

| 名称 | 说明 |
| --- | --- |
| TinkerFin | 智能体工作台 |

\`\`\`ts
const value = 42
\`\`\`
`

describe('MarkdownContent article contract', () => {
  it('preserves headings, three-level lists, blockquotes, separators and table semantics', () => {
    const { container } = render(<MarkdownContent content={completeFixture} />)

    expect(screen.getByRole('heading', { level: 1, name: '一级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: '二级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: '三级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 4, name: '四级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 5, name: '五级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 6, name: '六级标题' })).toBeInTheDocument()
    const paragraphs = container.querySelectorAll('.markdown-content > p')
    expect(paragraphs).toHaveLength(4)
    expect(paragraphs[0]).toHaveTextContent('第一段正文')
    expect(paragraphs[1]).toHaveTextContent('第二段正文')
    expect(container.querySelectorAll('ol ol, ol ul, ul ol, ul ul').length).toBeGreaterThanOrEqual(2)
    expect(container.querySelector('blockquote')).toHaveTextContent('一段引用')
    expect(container.querySelector('hr')).not.toBeNull()
    expect(screen.getByRole('table')).toHaveTextContent('TinkerFin')
    expect(screen.getByRole('region', { name: '可横向滚动的表格' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('link', { name: 'TinkerFin 前端规范文档' })).toHaveAttribute(
      'href',
      'https://example.com/docs/frontend/visual-system/markdown-contract-and-accessibility-checklist',
    )
  })

  it('renders inline code separately from a language-labelled code block', () => {
    const { container } = render(<MarkdownContent content={completeFixture} />)

    expect(container.querySelector('.markdown-content p code')).toHaveTextContent('const answer = 42')
    expect(container.querySelector('.markdown-code-block__head')).toHaveTextContent('ts')
    expect(container.querySelector('.markdown-code-block pre code')).toHaveTextContent('const value = 42')
  })

  it('keeps rendered task-list markers decorative instead of exposing disabled controls', () => {
    const { container } = render(<MarkdownContent content="- [ ] 浏览器回归" />)

    const checkbox = container.querySelector('input[type="checkbox"]')
    expect(checkbox).toHaveAttribute('aria-hidden', 'true')
    expect(checkbox).toHaveAttribute('tabindex', '-1')
    expect(screen.getByText('浏览器回归')).toBeInTheDocument()
  })

  it('copies code through the real client-side clipboard action', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(<MarkdownContent content={completeFixture} />)

    await user.click(screen.getByRole('button', { name: '复制' }))
    expect(writeText).toHaveBeenCalledWith('const value = 42')
    expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument()
  })

  it('exposes compact content as an explicit variant', () => {
    const { container } = render(<MarkdownContent content="**工具结果**" variant="compact" />)
    expect(container.firstElementChild).toHaveClass('markdown-content--compact')
  })

  it('owns the measured article rhythm while keeping compact Markdown isolated', () => {
    render(<MarkdownContent content={'第一段\n\n第二段'} />)

    expect(conversationStyles).toMatch(/\.markdown-content--article\s*\{[^}]*font-size:\s*var\(--type-conversation-body-size\);[^}]*line-height:\s*var\(--type-conversation-body-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article p\s*\{\s*margin:\s*var\(--space-4\) 0 var\(--space-1\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article h1\s*\{[^}]*font-size:\s*var\(--type-conversation-h1-size\);[^}]*line-height:\s*var\(--type-conversation-h1-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article h2\s*\{[^}]*font-size:\s*var\(--type-conversation-h2-size\);[^}]*line-height:\s*var\(--type-conversation-h2-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article h3\s*\{[^}]*font-size:\s*var\(--type-conversation-h3-size\);[^}]*line-height:\s*var\(--type-conversation-h3-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article h4\s*\{[^}]*font-size:\s*var\(--type-conversation-h4-size\);[^}]*line-height:\s*var\(--type-conversation-h4-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article h5\s*\{[^}]*font-size:\s*var\(--type-conversation-body-size\);[^}]*line-height:\s*var\(--type-conversation-body-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article h6\s*\{[^}]*font-size:\s*var\(--type-conversation-body-size\);[^}]*font-weight:\s*var\(--weight-regular\);[^}]*line-height:\s*var\(--type-conversation-body-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article blockquote\s*\{[^}]*margin:\s*0 0 var\(--space-2\);[^}]*padding:\s*var\(--space-2\) 0 var\(--space-2\) var\(--space-6\);[^}]*border-left:\s*0;/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article :not\(pre\) > code\s*\{[^}]*padding:\s*2\.4px 4\.8px;[^}]*font-size:\s*var\(--type-conversation-code-size\);[^}]*line-height:\s*var\(--type-conversation-code-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-code-block\s*\{[^}]*border-radius:\s*var\(--radius-3xl\);/s)
    expect(conversationStyles).not.toMatch(/\.markdown-content--article \.markdown-code-block\s*\{[^}]*border-radius:/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article \.markdown-table-wrap\s*\{[^}]*max-width:\s*none;[^}]*margin:\s*0 calc\(var\(--space-4\) \* -1\);[^}]*padding-inline:\s*var\(--space-4\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article th\s*\{[^}]*padding-top:\s*var\(--space-2\);[^}]*font-size:\s*var\(--type-conversation-table-size\);[^}]*line-height:\s*var\(--type-conversation-table-head-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--article td\s*\{[^}]*padding-top:\s*10px;[^}]*font-size:\s*var\(--type-conversation-table-size\);[^}]*line-height:\s*var\(--type-conversation-table-line\);/s)
    expect(conversationStyles).toMatch(/\.markdown-content--compact h1,[\s\S]*\.markdown-content--compact h6\s*\{/s)
    expect(conversationStyles).toMatch(/\.tool-rich-field \.markdown-content :is\(h1, h2, h3, h4, h5, h6\)/s)
  })
})


it('文档可关闭远端图片加载，聊天默认仍可展示 Markdown 图片', () => {
  const content = '![营收趋势](https://example.com/chart.png)'
  const { rerender } = render(<MarkdownContent content={content} allowRemoteImages={false} />)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.getByText('营收趋势')).toBeVisible()
  rerender(<MarkdownContent content={content} />)
  expect(screen.getByRole('img', { name: '营收趋势' })).toHaveAttribute('src', 'https://example.com/chart.png')
})
