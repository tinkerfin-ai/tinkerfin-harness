import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearAuthSession, saveAuthSession } from '../../auth/session'
import { subscribeApiErrors } from '../../api/shared/http'
import { useModelSettings } from './useModelSettings'

const jsonResponse = (data: unknown, status = 200) => new Response(JSON.stringify({
  code: status === 200 ? 0 : 500,
  message: status === 200 ? 'success' : '服务暂不可用',
  data,
}), {status, headers: {'Content-Type': 'application/json'}})

describe('模型异常全局通知', () => {
  const apiError = vi.fn()
  let unsubscribe: () => void
  beforeEach(() => {
    apiError.mockReset()
    saveAuthSession({token: 'test-model-feedback', serverAddress: 'http://127.0.0.1:8090', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user: {user_id: 1, username: 'test', display_name: 'Test', avatar_url: null, roles: [], disabled: false}})
    unsubscribe = subscribeApiErrors(apiError)
  })
  afterEach(() => {
    cleanup()
    unsubscribe()
    clearAuthSession()
    vi.unstubAllGlobals()
  })

  it('加载失败由统一请求层通知一次，重试成功恢复列表状态', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(jsonResponse(null, 503)).mockResolvedValueOnce(jsonResponse({ models: [], connections: [], providers: [] })))
    const hook = renderHook(() => useModelSettings())
    await waitFor(() => expect(hook.result.current.loadFailed).toBe(true))
    expect(apiError).toHaveBeenCalledOnce()
    expect(apiError.mock.calls[0][0].message).toBe('服务暂不可用')
    hook.rerender()
    expect(apiError).toHaveBeenCalledOnce()
    act(() => hook.result.current.reload())
    await waitFor(() => expect(hook.result.current.loading).toBe(false))
    expect(hook.result.current.loadFailed).toBe(false)
    expect(apiError).toHaveBeenCalledOnce()
  })

  it('取消模型加载后不通知晚到的服务错误', async () => {
    let respond!: (value: Response) => void
    const fetchMock = vi.fn(() => new Promise<Response>(resolve => { respond = resolve }))
    vi.stubGlobal('fetch', fetchMock)
    const hook = renderHook(() => useModelSettings())
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())
    hook.unmount()
    await act(async () => { respond(jsonResponse(null, 503)) })
    expect(apiError).not.toHaveBeenCalled()
  })
})
