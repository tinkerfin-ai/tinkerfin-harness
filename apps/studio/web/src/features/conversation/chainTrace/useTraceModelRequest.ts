import { useEffect, useState } from 'react'

import { fetchTraceModelRequest, type TraceModelRequest } from '../../../api/conversation/traceGraph'

type RequestState =
  | { phase: 'idle' }
  | { phase: 'loading' | 'error'; owner: string }
  | { phase: 'ready'; owner: string; detail: TraceModelRequest }

/** 请求正文只在查看模型详情时读取；切换节点或关闭详情会取消所属请求 */
export function useTraceModelRequest({ threadId, nodeId, reference, enabled }: {
  threadId: string
  nodeId: string
  reference?: string | null
  enabled: boolean
}) {
  const [state, setState] = useState<RequestState>({ phase: 'idle' })
  const [retryEpoch, setRetryEpoch] = useState(0)
  const owner = JSON.stringify([threadId, nodeId, reference])
  const loaded = state.phase === 'ready' && state.owner === owner

  useEffect(() => {
    if (!enabled || !reference || loaded) return
    const controller = new AbortController()
    setState({ phase: 'loading', owner })
    void fetchTraceModelRequest(threadId, reference, controller.signal).then(detail => {
      if (controller.signal.aborted) return
      setState(detail.nodeId === nodeId
        ? { phase: 'ready', owner, detail }
        : { phase: 'error', owner })
    }).catch(() => {
      if (!controller.signal.aborted) setState({ phase: 'error', owner })
    })
    return () => controller.abort()
  }, [enabled, loaded, nodeId, owner, reference, retryEpoch, threadId])

  const current: RequestState = enabled && reference
    ? state.phase !== 'idle' && state.owner === owner ? state : { phase: 'loading', owner }
    : { phase: 'idle' }
  return { state: current, retry: () => setRetryEpoch(value => value + 1) }
}
