import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { ContextMessage } from './ContextMessage'

afterEach(cleanup)

it('上下文附件可直接打开，来源内部标识不进入标题', () => {
  render(<ContextMessage message={{ id: 'context-with-file', role: 'context', createdAt: '2030-01-01T00:00:00Z',
    content: '报告补充内容', meta: { source: { kind: 'context', name: 'report-producer' } },
    attachments: [{ id: 'attachment', name: 'report.md', mime_type: 'text/markdown', size_bytes: 8 }],
  }} />)
  expect(screen.getByRole('button', { name: '预览文档：report.md' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '下载附件：report.md' })).toBeInTheDocument()
  expect(screen.getByText('附加上下文')).toBeInTheDocument()
  expect(screen.queryByText('report-producer')).not.toBeInTheDocument()
  expect(screen.queryByText('报告补充内容')).not.toBeInTheDocument()
})
