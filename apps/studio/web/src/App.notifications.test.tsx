import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'
import type { AuthUser } from './api/auth/types'
import { AUTH_SESSION_STORAGE_KEY, clearAuthSession, saveAuthSession, type AuthSession } from './auth/session'
import type { ToastHandler } from './components/ui/ToastViewport'

vi.mock('./features/workspace/WorkspaceScreen', () => ({
  WorkspaceScreen: ({ onToast, user }: { onToast: ToastHandler; user: { user_id: number } }) =>
    <button type="button" onClick={() => onToast('success', `账号 ${user.user_id} 的自动化已完成`, { label: '打开会话', onSelect: vi.fn() })}>在线完成</button>,
}))

afterEach(() => { cleanup(); clearAuthSession(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

it('外部身份切换清除完成消息与操作，切回原身份也不会补弹', async () => {
  vi.useFakeTimers()
  const user = { user_id: 7, username: 'owner', avatar_url: null, roles: [], disabled: false }
  const original: AuthSession = { serverAddress: 'http://127.0.0.1:8090', token: 'original-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z', user }
  saveAuthSession(original)
  let activeUser: AuthUser = user
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code: 0, message: 'success', data: { expires_at: original.expiresAt, user: activeUser } }), { headers: { 'Content-Type': 'application/json' } })))
  const media = window.matchMedia
  vi.stubGlobal('matchMedia', (query: string) => ({ ...media(query), matches: query === '(prefers-reduced-motion: reduce)' }))
  await act(async () => { render(<App />); await vi.advanceTimersByTimeAsync(0) })
  fireEvent.click(screen.getByRole('button', { name: '在线完成' }))
  expect(screen.getByText('账号 7 的自动化已完成')).toBeVisible()
  for (const next of [{ ...original, token: 'other-token', user: { ...user, user_id: 8 } }, original]) {
    activeUser = next.user
    await act(async () => {
      localStorage.setItem(AUTH_SESSION_STORAGE_KEY, JSON.stringify(next))
      window.dispatchEvent(new StorageEvent('storage', { key: AUTH_SESSION_STORAGE_KEY }))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByRole('button', { name: '在线完成' })).toBeVisible()
    expect(screen.queryByText('账号 7 的自动化已完成')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开会话' })).not.toBeInTheDocument()
  }
})
