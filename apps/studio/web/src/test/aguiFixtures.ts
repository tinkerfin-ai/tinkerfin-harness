import type { InterruptEvent } from '../api/conversation/types'
import type { JsonObject } from '../types'
import type { ToolReviewDecision } from '../features/conversation/agui/toolReviewContract'

/** 构造用于验证审批界面的公开协议样本 */
export const toolReviewInterrupts = (
  nativeInterruptId: string,
  actions: Array<{
    toolCallId: string
    name?: string
    args: JsonObject
    description?: string
    allowedDecisions?: ToolReviewDecision[]
  }>,
): InterruptEvent[] => {
  const actionRequests = actions.map(action => ({
    name: action.name ?? 'write_file',
    args: action.args,
    ...(action.description == null ? {} : { description: action.description }),
  }))
  const reviewConfigs = actions.map(action => ({
    action_name: action.name ?? 'write_file',
    allowed_decisions: action.allowedDecisions ?? ['approve', 'reject'],
  }))
  return actions.map((action, index) => ({
    id: actions.length === 1 ? nativeInterruptId : `${nativeInterruptId}#${index}`,
    reason: 'tool_call',
    toolCallId: action.toolCallId,
    message: action.description ?? action.name ?? 'write_file',
    metadata: {
      deepagents: {
        schema: 'tinkerfin.deepagents.tool-review',
        nativeInterruptId,
        actionIndex: index,
        toolName: action.name ?? 'write_file',
        allowedDecisions: action.allowedDecisions ?? ['approve', 'reject'],
        originalArgs: action.args,
      },
      langgraphValue: { action_requests: actionRequests, review_configs: reviewConfigs },
    },
  }))
}

/** 构造与实时澄清或计划审阅相同的公开事件 */
export const planInterrupt = (id: string, envelope: JsonObject): InterruptEvent => ({
  id,
  reason: String(envelope.kind),
  message: typeof envelope.message === 'string' ? envelope.message : undefined,
  responseSchema: envelope.responseSchema as JsonObject,
  metadata: {
    runtimeInterrupt: {
      schema: 'tinkerfin.runtime-interrupt',
      nativeInterruptId: id,
      envelope,
    },
  },
})
