import { render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { Message } from '../../../types'
import { buildEmptyConversation } from '../../../lib/workspace'
import { ToolCallCard } from '../components/MessageBlock'
import { ToolCallRow } from '../components/ToolCallRow'
import { buildConversationDisplayEntries } from '../todoTrace/displayEntries'

it('整批交付被拒绝时，折叠行保留真实失败原因', () => {
  render(<ToolCallRow children={null} message={{
    id: 'denied', role: 'tool', content: 'deliver_files', createdAt: '',
    meta: { toolName: 'deliver_files', status: 'failed', result: '无权读取该文件',
      params: JSON.stringify({ files: [{ file_path: '/private.csv', name: 'private.csv' }] }) },
  }} />)
  expect(screen.getByText('无权读取该文件')).toBeVisible()
})

it('批量结果同时保留成功附件与失败原因，不展示重复 JSON', () => {
  const attachment = { id: 'csv', name: '结果.csv', mime_type: 'text/csv', size_bytes: 80 }
  const message: Message = {
    id: 'batch', role: 'tool', content: 'deliver_files', createdAt: '',
    attachments: [attachment],
    meta: {
      toolName: 'computer_deliver_files', status: 'completed',
      result: JSON.stringify({ results: [
        { file_path: '/work/result.csv', name: attachment.name, status: 'delivered', attachment_id: attachment.id },
        { file_path: '/work/missing.txt', name: '说明.txt', status: 'failed', error: '原文件不存在' },
      ] }),
    },
  }
  render(<ToolCallCard message={message} />)
  expect(screen.getByText('已交付 1 个文件 · 1 项失败')).toBeVisible()
  expect(screen.getByRole('button', { name: '预览文档：结果.csv' })).toBeVisible()
  expect(within(screen.getByRole('list', { name: '未交付的文件' })).getByText('原文件不存在')).toBeVisible()
  expect(screen.queryByText(/"attachment_id"/)).not.toBeInTheDocument()
})

it('整组审批的工具只在审批面板呈现，完成后恢复历史工具记录', () => {
  const conversation = buildEmptyConversation({ projectId: 'project', threadId: 'thread', now: '2026-10-23T00:00:00Z' })
  const items = ['first', 'second'].map(id => ({
    id, interruptId: id, toolCallId: id, toolName: 'write_file', params: '{}',
    input: '', description: '', originalArgs: {}, allowedDecisions: ['approve', 'reject'] as Array<'approve' | 'reject'>,
  }))
  conversation.messages = items.map(item => ({
    id: item.id, role: 'tool', content: item.toolName, createdAt: '',
    meta: { toolName: item.toolName, toolCallId: item.toolCallId, status: 'paused' },
  }))
  conversation.approval = { items, activeIndex: 0, submitted: false }
  expect(buildConversationDisplayEntries(conversation)).toEqual([])
  conversation.approval.submitted = true
  expect(buildConversationDisplayEntries(conversation)).toEqual([])
  conversation.approval = undefined
  expect(buildConversationDisplayEntries(conversation).flatMap(entry => entry.type === 'tools' ? entry.messages : [])).toEqual(conversation.messages)
})
