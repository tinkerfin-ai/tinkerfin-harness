import type { ConversationHistoryDetail, InteractionAvailability, InteractionSubmissionResult } from '../../api/conversation/history'
import { parsePlanResults } from '../../api/conversation/planResults'
import type { Conversation, DeepReadonly, Message } from '../../types'
import { translateCurrent } from '../../i18n'

type InteractionView = Pick<Conversation, 'approval' | 'planInteraction' | 'pendingInteractionKind'>

export const interactionInterruptIds = (view: InteractionView): string[] => view.approval
  ? view.approval.items.map(item => item.interruptId)
  : view.planInteraction ? [view.planInteraction.interruptId] : []

const sameInterrupts = (left: readonly string[], right: readonly string[]) => left.length > 0
  && left.length === right.length && new Set(left).size === left.length
  && new Set(right).size === right.length && left.every(id => right.includes(id))

/** 先确认旧提交是否结束，再依据当前完整权限决定能否操作或需要跟随哪次提交 */
export function resolveInteractionSubmission(
  view: InteractionView,
  availability: readonly DeepReadonly<InteractionAvailability>[],
  result: DeepReadonly<InteractionSubmissionResult> | null,
): { view: InteractionView; state: 'unknown' | 'available' | 'confirming' | 'settled'; notSaved: boolean; submissionRunId?: string } {
  const ids = interactionInterruptIds(view)
  const form = view.approval ?? view.planInteraction
  if (!form || !ids.length) return { view, state: 'unknown', notSaved: false }
  const notSaved = Boolean(form.submitted && form.submissionRunId && result
    && result.submissionRunId === form.submissionRunId && sameInterrupts(ids, result.interruptIds))
  const values = ids.map(id => availability.find(item => item.interruptId === id))
  const complete = values.every(item => item !== undefined)
  const owned = form.submitted && form.submissionRunId && !notSaved
  const matches = complete && (!owned || values.every(item => item?.submissionRunId === form.submissionRunId))
  const contradictory = notSaved && values.some(item => item?.submissionRunId === form.submissionRunId)
  const owners = new Set(values.map(item => item?.submissionRunId).filter((id): id is string => Boolean(id)))
  const state = !matches || contradictory ? 'unknown'
    : values.every(item => item?.state === 'available') ? 'available'
      : owners.size === 1 && values.every(item => item?.state === 'resolved' || item?.state === 'cancelled') ? 'settled'
        : 'confirming'
  const submissionRunId = state === 'available' ? undefined
    : state === 'unknown' ? form.submissionRunId
      : owners.size === 1 ? [...owners][0] : undefined
  if (state === 'settled') return { view: {}, state, notSaved, submissionRunId }
  const patch = {
    submitted: state !== 'available', submissionRunId,
    requestRejected: state === 'available' || (notSaved && state !== 'unknown') ? undefined : form.requestRejected,
    error: notSaved && state === 'available' ? translateCurrent('提交未保存，请重试')
      : notSaved && state !== 'unknown' ? undefined : form.error,
  }
  return { state, notSaved, submissionRunId, view: {
    ...view,
    approval: view.approval ? { ...view.approval, ...patch } : undefined,
    planInteraction: view.planInteraction ? { ...view.planInteraction, ...patch } : undefined,
  } }
}

/** 仅首次提交在收到任何事件前被明确拒绝时调用，回读当前权限前仍保持只读 */
export function markInteractionRequestRejected(
  conversation: Conversation,
  threadId: string,
  submissionRunId: string,
  interruptIds: readonly string[],
): Conversation {
  const form = conversation.approval ?? conversation.planInteraction
  if (conversation.threadId !== threadId || !form?.submitted || form.submissionRunId !== submissionRunId
    || !sameInterrupts(interactionInterruptIds(conversation), interruptIds) || form.requestRejected) return conversation
  return { ...conversation,
    approval: conversation.approval ? { ...conversation.approval, requestRejected: true } : undefined,
    planInteraction: conversation.planInteraction ? { ...conversation.planInteraction, requestRejected: true } : undefined,
  }
}

/** 交互记录归于续跑正文之前，两种确认入口保持相同时间顺序 */
export function upsertConfirmedPlanHistory(messages: Message[], entry: Message, submissionRunId: string | undefined): Message[] {
  const previousIndex = messages.findIndex(item => item.id === entry.id)
  const resumedIndex = submissionRunId ? messages.findIndex(item => item.meta?.runId === submissionRunId) : -1
  const next = [...messages]
  if (previousIndex >= 0) next[previousIndex] = entry
  else next.splice(resumedIndex >= 0 ? resumedIndex : next.length, 0, entry)
  return next
}

/** 保存确认只收束对应提交，不用历史快照回退实时正文、运行状态或游标 */
export function applyInteractionConfirmation(
  conversation: Conversation,
  detail: ConversationHistoryDetail,
  submissionRunId: string,
  rejectedInterruptIds?: readonly string[],
): Conversation {
  const form = conversation.approval ?? conversation.planInteraction
  if (detail.threadId !== conversation.threadId || !form?.submitted
    || form.submissionRunId !== submissionRunId) return conversation
  const ids = interactionInterruptIds(conversation)
  const availability = ids.map(id => detail.interactionAvailability.find(item => item.interruptId === id))
  // 新请求的明确 HTTP 拒绝只证明该请求失败；仍须使用拒绝之后重新读取的当前权限
  const submissionResult = form.requestRejected && rejectedInterruptIds && sameInterrupts(ids, rejectedInterruptIds)
    ? { submissionRunId, interruptIds: [...rejectedInterruptIds], state: 'not_saved' as const }
    : detail.submissionResult
  const resolution = resolveInteractionSubmission(conversation, detail.interactionAvailability, submissionResult)
  if (resolution.state === 'unknown' || (!resolution.notSaved && resolution.state !== 'settled')) return conversation
  const result = parsePlanResults(detail.planResults).filter(item => ids.includes(item.interruptId)
    && item.submissionRunId === resolution.submissionRunId)
  const trace = conversation.trace ? { ...conversation.trace,
    interactionAvailability: [...conversation.trace.interactionAvailability.filter(item => !ids.includes(item.interruptId)),
      ...detail.interactionAvailability.filter(item => ids.includes(item.interruptId))],
    planResults: [...conversation.trace.planResults.filter(item => !ids.includes(item.interruptId)), ...result],
    submissionResult,
  } : undefined
  let messages = conversation.messages
  const plan = conversation.planInteraction
  const origin = plan && (conversation.trace?.interactions.find(item => item.agui?.some(action => action.id === plan.interruptId))
    ?? detail.interactions.find(item => item.agui?.some(action => action.id === plan.interruptId)))
  if (resolution.state === 'settled' && plan && origin) {
    const entry: Message = {
      id: `plan-history:${plan.interruptId}`, role: 'process', content: '', createdAt: origin.openedAt,
      meta: { planHistory: structuredClone(plan), planResult: result.find(item => item.interruptId === plan.interruptId),
        runId: origin.runId, status: availability[0]?.state === 'cancelled' ? 'cancelled' : 'completed' },
    }
    messages = upsertConfirmedPlanHistory(messages, entry, resolution.submissionRunId)
  }
  return { ...conversation, messages, trace,
    approval: resolution.view.approval, planInteraction: resolution.view.planInteraction,
    pendingInteractionKind: resolution.view.pendingInteractionKind,
    notice: conversation.notice?.id?.endsWith(':interaction-confirmation') ? undefined : conversation.notice,
  }
}
