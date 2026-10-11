import { useCallback, useEffect, useRef } from 'react'
import { getServerAddress, subscribeServerAddress } from '../../api/shared/config'
import { getAuthorizationHeader, subscribeAuthSession } from '../../auth/session'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { fetchRunStatus } from './api'
import type { AutomationRunRecord, RunConversation } from './model'

export type OpenRunConversation = (
  run: Pick<AutomationRunRecord, 'id' | 'projectId'>,
  options?: { signal?: AbortSignal; prepareDraft?: (target: RunConversation) => boolean },
) => Promise<boolean>

/** 详情和完成通知都重新确认会话位置；请求退出原身份或页面后不再导航 */
export function useRunConversation(onOpen: (target: RunConversation) => void, onToast?: ToastHandler): OpenRunConversation {
  const { t } = useI18n()
  const latest = useRef({ onOpen, onToast, t })
  latest.current = { onOpen, onToast, t }
  const pending = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    let identity = `${getServerAddress()}\n${getAuthorizationHeader()}`
    const cancel = () => { pending.current?.abort(); pending.current = null }
    const changed = () => {
      const next = `${getServerAddress()}\n${getAuthorizationHeader()}`
      if (next !== identity) { identity = next; cancel() }
    }
    const releaseAuth = subscribeAuthSession(changed)
    const releaseServer = subscribeServerAddress(changed)
    window.addEventListener('popstate', cancel)
    return () => { mounted.current = false; cancel(); releaseAuth(); releaseServer(); window.removeEventListener('popstate', cancel) }
  }, [])
  return useCallback<OpenRunConversation>(async (run, options) => {
    pending.current?.abort()
    if (!mounted.current || options?.signal?.aborted) return false
    const controller = new AbortController()
    pending.current = controller
    const cancel = () => controller.abort()
    options?.signal?.addEventListener('abort', cancel, { once: true })
    const location = window.location.href
    const identity = `${getServerAddress()}\n${getAuthorizationHeader()}`
    const current = () => !controller.signal.aborted && pending.current === controller
      && window.location.href === location && identity === `${getServerAddress()}\n${getAuthorizationHeader()}`
    try {
      const status = await fetchRunStatus(run.projectId, run.id, controller.signal)
      if (!current()) return false
      const { onOpen, onToast, t } = latest.current
      if (status.conversation.state !== 'available') {
        onToast?.('error', t(status.conversation.state === 'deleted' ? '关联会话已删除'
          : status.conversation.state === 'pending' ? '关联会话尚未开放' : '关联会话尚未创建'))
        return false
      }
      if (options?.prepareDraft && !options.prepareDraft(status.conversation)) return false
      onOpen(status.conversation)
      return true
    } catch {
      if (current()) latest.current.onToast?.('error', latest.current.t('关联会话加载失败，请重试'))
      return false
    } finally {
      options?.signal?.removeEventListener('abort', cancel)
      if (pending.current === controller) pending.current = null
    }
  }, [])
}
