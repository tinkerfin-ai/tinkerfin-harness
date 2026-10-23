import { useSyncExternalStore } from 'react'
import type { ActionResult, DesktopAction, DesktopState } from './state'

declare global {
  interface Window {
    webkit?: { messageHandlers: { desktop: { postMessage: (message: DesktopAction) => Promise<unknown> } } }
    receiveDesktopState: (state: unknown) => void
  }
}

let state: DesktopState | null = null
const listeners = new Set<() => void>()
const text = (value: unknown): value is string => typeof value === 'string'
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const oneOf = (value: unknown, choices: readonly string[]) => text(value) && choices.includes(value)

function validState(value: unknown): value is DesktopState {
  if (!record(value) || !record(value.permissions) || !record(value.update)) return false
  return Number.isSafeInteger(value.revision) && Number(value.revision) >= 0
    && oneOf(value.surface, ['workbench', 'status', 'updater'])
    && oneOf(value.language, ['system', 'zh-Hans', 'en']) && oneOf(value.locale, ['zh-Hans', 'en'])
    && oneOf(value.theme, ['light', 'dark'])
    && ['ready', 'connected', 'switching', 'projectsLoading'].every(key => typeof value[key] === 'boolean')
    && ['service', 'api', 'name', 'account', 'version', 'openingProject', 'authorizationError', 'connectionError'].every(key => text(value[key]))
    && oneOf(value.connection, ['loading', 'unpaired', 'authorizing', 'connecting', 'online', 'offline', 'revoked'])
    && oneOf(value.connectionReason, ['restore', 'authorization', 'retry'])
    && oneOf(value.authorizationPhase, ['idle', 'starting', 'waiting', 'cancelling'])
    && oneOf(value.busy, ['', 'name', 'logout', 'connect', 'retry'])
    && typeof value.permissions.accessibility === 'boolean' && typeof value.permissions.screenCapture === 'boolean'
    && Array.isArray(value.projects) && value.projects.length <= 8192
    && value.projects.every(project => record(project) && text(project.id) && text(project.name))
    && oneOf(value.update.phase, ['idle', 'checking', 'available', 'latest', 'check-error', 'downloading', 'download-error', 'verifying', 'verify-error', 'waiting', 'installing', 'install-error', 'restarting', 'restart-error', 'success'])
    && typeof value.update.percent === 'number' && Number.isFinite(value.update.percent) && value.update.percent >= 0 && value.update.percent <= 100
    && text(value.update.currentVersion) && text(value.update.targetVersion)
    && oneOf(value.update.failure, ['none', 'network', 'storage', 'release', 'unavailable'])
    && (value.resume === null || (record(value.resume) && oneOf(value.resume.page, ['connection', 'workspace', 'permissions', 'account'])
      && text(value.resume.service) && text(value.resume.api) && text(value.resume.name)
      && typeof value.resume.manualAPI === 'boolean' && typeof value.resume.editingName === 'boolean' && typeof value.resume.connectionForm === 'boolean'))
}

/** 桥接只接受原生发布的无凭证快照；序号防止迟到快照回退界面 */
window.receiveDesktopState = value => {
  if (!validState(value) || (state && value.revision <= state.revision)) return
  state = value
  listeners.forEach(listener => listener())
}

export function useDesktopState() {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener) } }, () => state)
}

export async function send(action: DesktopAction): Promise<ActionResult> {
  const channel = window.webkit?.messageHandlers.desktop
  if (!channel) return { ok: false, message: '界面暂不可用', field: '' }
  try {
    const result = await channel.postMessage(action)
    if (record(result) && result.ok === true) return { ok: true }
    if (record(result) && result.ok === false && text(result.message) && oneOf(result.field, ['', 'service', 'api', 'name'])) {
      return { ok: false, message: result.message, field: result.field as '' | 'service' | 'api' | 'name' }
    }
  } catch { /* 窗口关闭或原生请求失败不改变已确认状态，用户可再次提交 */ }
  return { ok: false, message: '请重试', field: '' }
}
