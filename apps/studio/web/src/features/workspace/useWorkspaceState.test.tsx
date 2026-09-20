import { act, renderHook } from '@testing-library/react'
import { StrictMode, type PropsWithChildren } from 'react'
import { describe, expect, it } from 'vitest'

import { buildEmptyConversation, removeConversation, selectCurrentConversation, updateConversation, upsertConversation } from '../../lib/workspace'
import { emptyTraceGraph } from '../../test/traceFixtures'
import type { Conversation } from '../../types'
import { useWorkspaceState, type RetainConversationDetails } from './useWorkspaceState'

const NOW = '2026-09-20T00:00:00Z'

const ended = (threadId: string, overrides: Partial<Conversation> = {}): Conversation => ({
  ...buildEmptyConversation({ threadId, now: NOW, model: 'server-model' }),
  title: `会话 ${threadId}`,
  historySynchronized: true,
  messages: [{ id: `${threadId}:answer`, role: 'assistant', content: `答复 ${threadId}`, createdAt: NOW }],
  serverState: { files: { result: '内容' } },
  lastSeq: 2,
  trace: {
    id: 1,
    threadId,
    title: `会话 ${threadId}`,
    titleSource: 'generated',
    titleGenerationStatus: 'succeeded',
    titleSeq: 1,
    lastModel: 'server-model',
    accessMode: 'full',
    pinned: false,
    asOfSeq: 2,
    generation: 'fixture',
    observedAt: NOW,
    headRunId: `${threadId}:run`,
    availableHeads: [`${threadId}:run`],
    messageCount: 1,
    toolCallCount: 0,
    historyCursor: null,
    messages: [{
      id: `${threadId}:answer`,
      sourceId: `${threadId}:answer`,
      traceSeq: 1,
      graphNamespace: [],
      runId: `${threadId}:run`,
      role: 'assistant',
      content: `答复 ${threadId}`,
      contentOmitted: false,
      status: 'completed',
      createdAt: NOW,
      completedAt: NOW,
      agui: { kind: 'message', messageId: `${threadId}:answer` },
    }],
    reasoning: [],
    interactions: [],
    runFailures: [],
    graph: emptyTraceGraph(2),
    state: { root: { files: { result: '内容' } }, subgraphs: {} },
    status: { execution: 'succeeded', headRunId: `${threadId}:run` },
    completeness: { missingPrefix: false, missingTail: false, payloadOmitted: false },
    createdAt: NOW,
    updatedAt: NOW,
  },
  ...overrides,
})

const strictWrapper = ({ children }: PropsWithChildren) => <StrictMode>{children}</StrictMode>

const harness = () => {
  const hook = renderHook(useWorkspaceState, { wrapper: strictWrapper })
  const visit = (conversation: Conversation) => act(() => hook.result.current.setWorkspace(state => (
    upsertConversation(selectCurrentConversation(state, conversation.threadId), conversation)
  )))
  const get = (threadId: string) => hook.result.current.workspace.conversations.find(item => item.threadId === threadId)
  const residentIds = () => hook.result.current.workspace.conversations.filter(item => item.trace).map(item => item.threadId).sort()
  return { ...hook, visit, get, residentIds }
}

describe('工作台会话详情缓存', () => {
  it('当前已结束会话计入三个名额，重访提升访问顺序，后台更新不提升', () => {
    const { result, visit, get, residentIds } = harness()
    for (const id of ['a', 'b', 'c']) visit(ended(id))
    act(() => result.current.setWorkspace(state => selectCurrentConversation(state, 'a')))
    act(() => result.current.setWorkspace(state => updateConversation(state, 'b', item => ({
      ...item, title: '后台更新的标题', updatedAt: '2026-09-21T00:00:00Z',
    }))))
    visit(ended('d'))
    expect(residentIds()).toEqual(['a', 'c', 'd'])
    expect(get('b')).toMatchObject({ title: '后台更新的标题', messages: [], isHydrated: false, historySynchronized: false })
    expect(get('b')?.trace).toBeUndefined()
    expect(get('b')?.serverState).toBeUndefined()
    expect(get('b')?.lastSeq).toBeUndefined()
  })

  it('摘要已标记待重载但仍持有正文的会话仍占详情额度', () => {
    const { result, visit, get, residentIds } = harness()
    for (const id of ['a', 'b', 'c']) visit(ended(id))
    act(() => result.current.setWorkspace(state => updateConversation(state, 'a', item => ({ ...item, isHydrated: false }))))
    visit(ended('d'))
    expect(residentIds()).toEqual(['b', 'c', 'd'])
    expect(get('a')?.messages).toEqual([])
  })

  it.each([
    ['正在运行', { runStatus: 'streaming', historySynchronized: false }],
    ['断线待恢复', { runStatus: 'detached', historySynchronized: false }],
    ['终态尚未确认保存', { historySynchronized: false }],
    ['历史恢复失败', { notice: { kind: 'error', content: '需要恢复历史', recovery: 'history' } }],
    ['待审批', { runStatus: 'waiting_approval', approval: { items: [], activeIndex: 0, submitted: false, mode: 'options' } }],
    ['待回答', { pendingInteractionKind: 'plan_clarification' }],
  ] satisfies Array<[string, Partial<Conversation>]>)('%s 会话额外保留，不挤占三个已结束会话', (_name, overrides) => {
    const { visit, get, residentIds } = harness()
    const protectedConversation = ended('protected', overrides)
    visit(protectedConversation)
    for (const id of ['a', 'b', 'c', 'd']) visit(ended(id))
    expect(residentIds()).toEqual(['b', 'c', 'd', 'protected'])
    expect(get('protected')?.messages).toBe(protectedConversation.messages)
  })

  it('历史状态未知时不把错误展示当成已结束', () => {
    const { visit, residentIds } = harness()
    const conversation = ended('unknown', { runStatus: 'error' })
    visit({ ...conversation, trace: { ...conversation.trace!, status: { execution: 'unknown', headRunId: 'unknown:run' } } })
    for (const id of ['a', 'b', 'c', 'd']) visit(ended(id))
    expect(residentIds()).toEqual(['b', 'c', 'd', 'unknown'])
  })

  it('同会话的独立保护须全部释放，重复释放不会解除其他操作的保护', () => {
    const { result, visit, get } = harness()
    visit(ended('a'))
    let first!: ReturnType<RetainConversationDetails>
    let second!: ReturnType<RetainConversationDetails>
    act(() => {
      first = result.current.retainConversationDetails('a')
      second = result.current.retainConversationDetails('a')
    })
    for (const id of ['b', 'c', 'd']) visit(ended(id))
    act(() => { first.release(); first.release() })
    expect(get('a')?.trace).toBeDefined()
    act(() => {
      result.current.setWorkspace(state => updateConversation(state, 'a', item => ({ ...item, title: '收尾已提交' })))
      second.release()
    })
    expect(get('a')).toMatchObject({ title: '收尾已提交', messages: [], isHydrated: false })
  })

  it('草稿保护可以在首次写入前绑定正式会话', () => {
    const { result, visit, get } = harness()
    let retained!: ReturnType<RetainConversationDetails>
    act(() => { retained = result.current.retainConversationDetails('') })
    act(() => { retained.moveTo('a') })
    for (const id of ['a', 'b', 'c', 'd']) visit(ended(id))
    expect(get('a')?.trace).toBeDefined()
    act(() => retained.release())
    expect(get('a')?.trace).toBeUndefined()
  })

  it('未受理的模型、模式和权限选择跨淘汰及重新水化保留', () => {
    const { result, visit, get } = harness()
    visit(ended('a'))
    act(() => result.current.setComposerPreference('a', { model: 'local-model', mode: 'plan', accessMode: 'write_approval' }))
    for (const id of ['b', 'c', 'd']) visit(ended(id))
    expect(get('a')?.trace).toBeUndefined()
    visit(ended('a'))
    expect(get('a')).toMatchObject({ model: 'local-model', mode: 'plan', accessMode: 'write_approval', historySynchronized: true })
  })

  it('受理只消费本次提交的设置，提交后修改的字段继续覆盖后续快照', () => {
    const { result, visit, get } = harness()
    visit(ended('a'))
    const submitted = { model: 'submitted-model', mode: 'plan', accessMode: 'write_approval' } as const
    act(() => result.current.setComposerPreference('a', submitted))
    act(() => {
      result.current.setComposerPreference('a', { model: 'next-model' })
      result.current.acknowledgeComposerPreferences('a', submitted)
      result.current.setWorkspace(state => upsertConversation(state, ended('a')))
    })
    expect(get('a')).toMatchObject({ model: 'next-model', mode: 'default', accessMode: 'full' })
  })

  it('删除会话清除设置和保护，迟到释放不能恢复已删除内容', () => {
    const { result, visit, get } = harness()
    visit(ended('a'))
    let retained!: ReturnType<RetainConversationDetails>
    act(() => {
      retained = result.current.retainConversationDetails('a')
      result.current.setComposerPreference('a', { model: 'local-model' })
      result.current.setWorkspace(state => removeConversation(state, 'a'))
    })
    act(() => { retained.moveTo('a'); retained.release() })
    expect(get('a')).toBeUndefined()
    visit(ended('a'))
    expect(get('a')?.model).toBe('server-model')
    for (const id of ['b', 'c', 'd']) visit(ended(id))
    expect(get('a')?.trace).toBeUndefined()
  })

  it('工作台卸载后旧操作失效，新工作台不继承详情或设置', () => {
    const previous = harness()
    previous.visit(ended('a'))
    const oldApi = previous.result.current
    let retained!: ReturnType<RetainConversationDetails>
    act(() => {
      retained = oldApi.retainConversationDetails('a')
      oldApi.setComposerPreference('a', { model: 'old-model' })
    })
    previous.unmount()
    const next = harness()
    act(() => {
      retained.release()
      oldApi.setWorkspace({ currentThreadId: 'a', conversations: [ended('a')] })
    })
    expect(next.result.current.workspace.conversations).toEqual([])
    next.visit(ended('a'))
    expect(next.get('a')?.model).toBe('server-model')
  })
})
