import DOMPurify from 'dompurify'

/** 只保留文档排版与明确的网页链接，移除脚本、嵌入资源和页面控制属性 */
export function sanitizeDocumentHtml(html: string, tableLabelId: string): string {
  const fragment = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'strong', 'em', 's', 'del', 'table', 'thead', 'tbody', 'tr', 'td', 'th', 'blockquote', 'pre', 'code', 'br', 'a', 'sup', 'sub'],
    ALLOWED_ATTR: ['href', 'colspan', 'rowspan'],
    ALLOWED_URI_REGEXP: /^https?:\/\//i,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    RETURN_DOM_FRAGMENT: true,
  })
  for (const link of fragment.querySelectorAll('a[href]')) {
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
  }
  // 表格名称由预览界面提供，切换语言无需重新读取文档
  for (const table of fragment.querySelectorAll('table')) {
    const scroll = document.createElement('div')
    scroll.className = 'markdown-table-wrap'
    scroll.setAttribute('role', 'region')
    scroll.setAttribute('tabindex', '0')
    scroll.setAttribute('aria-labelledby', tableLabelId)
    table.replaceWith(scroll)
    scroll.append(table)
  }
  const container = document.createElement('div')
  container.append(fragment)
  return container.innerHTML
}
