import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { MarkdownContent } from './MarkdownContent'

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

  it('copies code through the real client-side clipboard action', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(<MarkdownContent content={completeFixture} />)

    await user.click(screen.getByRole('button', { name: '复制' }))
    expect(writeText).toHaveBeenCalledWith('const value = 42')
    expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument()
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
