import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TraceModelRequest } from '../../../api/conversation/traceGraph'
import { useTraceModelRequest } from './useTraceModelRequest'

const fetchRequest = vi.hoisted(() => vi.fn())
vi.mock('../../../api/conversation/traceGraph', () => ({ fetchTraceModelRequest: fetchRequest }))

describe('模型请求详情', () => {
  beforeEach(() => { fetchRequest.mockReset() })

  it('只在打开所选请求时读取，关闭详情取消读取', async () => {
    fetchRequest.mockReturnValue(new Promise(() => {}))
    const { result, rerender } = renderHook(({ enabled }) => useTraceModelRequest({
      threadId: 'thread', nodeId: 'model', reference: 'reference', enabled,
    }), { initialProps: { enabled: false } })
    expect(result.current.state.phase).toBe('idle')
    expect(fetchRequest).not.toHaveBeenCalled()
    rerender({ enabled: true })
    await waitFor(() => expect(fetchRequest).toHaveBeenCalledOnce())
    const signal = fetchRequest.mock.calls[0][2] as AbortSignal
    rerender({ enabled: false })
    expect(signal.aborted).toBe(true)
    expect(result.current.state.phase).toBe('idle')
  })

  it('切换节点后旧响应不能覆盖当前请求，失败可以重试', async () => {
    let resolveFirst!: (value: TraceModelRequest) => void
    fetchRequest.mockImplementationOnce(() => new Promise<TraceModelRequest>(resolve => { resolveFirst = resolve }))
      .mockRejectedValueOnce(new Error('unavailable'))
      .mockResolvedValueOnce({ nodeId: 'second', request: { messages: [] }, requestOmitted: false })
    const { result, rerender } = renderHook(({ nodeId }) => useTraceModelRequest({
      threadId: 'thread', nodeId, reference: nodeId, enabled: true,
    }), { initialProps: { nodeId: 'first' } })
    rerender({ nodeId: 'second' })
    await waitFor(() => expect(result.current.state.phase).toBe('error'))
    await act(async () => { resolveFirst({ nodeId: 'first', request: {}, requestOmitted: false }) })
    expect(result.current.state.phase).toBe('error')
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    if (result.current.state.phase === 'ready') expect(result.current.state.detail.nodeId).toBe('second')
  })

  it('拒绝与所选节点不匹配的详情', async () => {
    fetchRequest.mockResolvedValue({ nodeId: 'another', request: {}, requestOmitted: false })
    const { result } = renderHook(() => useTraceModelRequest({
      threadId: 'thread', nodeId: 'model', reference: 'reference', enabled: true,
    }))
    await waitFor(() => expect(result.current.state.phase).toBe('error'))
  })

  it('同一节点的不可变请求读取成功后，切回页签复用内容', async () => {
    fetchRequest.mockResolvedValue({ nodeId: 'model', request: {}, requestOmitted: false })
    const { result, rerender } = renderHook(({ enabled }) => useTraceModelRequest({
      threadId: 'thread', nodeId: 'model', reference: 'reference', enabled,
    }), { initialProps: { enabled: true } })
    await waitFor(() => expect(result.current.state.phase).toBe('ready'))
    rerender({ enabled: false })
    rerender({ enabled: true })
    expect(result.current.state.phase).toBe('ready')
    expect(fetchRequest).toHaveBeenCalledOnce()
  })
})
