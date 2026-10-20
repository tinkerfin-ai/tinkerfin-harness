import type { ConversationHistoryCoreDetail } from '../../../api/conversation/history'
import { ApiError } from '../../../api/shared/http'

export type ConversationObservation = Pick<ConversationHistoryCoreDetail, 'generation' | 'headRunId' | 'asOfSeq'>

export type HistoryRefreshResult =
  | { phase: 'ready'; observation: ConversationObservation }
  | { phase: 'failed' | 'unavailable' }

export interface HistoryActivationRefresh {
  /** 同一次前台恢复共享编号，视图激活和查询条件分别由链路请求持有 */
  epoch: number
  phase: 'pending' | HistoryRefreshResult['phase']
}

/** 会话不存在也可能表示不属于当前用户，不能继续展示先前读取的详情 */
export const isConversationUnavailable = (error: unknown) => (
  error instanceof ApiError
  && (error.isAuthError || [401, 403, 404].includes(error.status))
)
