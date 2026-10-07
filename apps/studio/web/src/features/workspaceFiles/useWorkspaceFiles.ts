import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { getAuthorizationHeader, subscribeAuthSession } from '../../auth/session'
import { getServerAddress, subscribeServerAddress } from '../../api/shared/config'
import { ApiError } from '../../api/shared/http'
import { startResourceFeed, type ResourceFeedState } from '../../api/shared/resourceFeed'
import { SseError } from '../../api/shared/sse'
import {
  readWorkspaceDirectory, readWorkspaceFileInfo, readWorkspacePreview, workspaceEndpoint, WORKSPACE_ERRORS,
  type WorkspaceFile, type WorkspacePreview,
} from './api'

export interface DirectoryState {
  entries: WorkspaceFile[]
  phase: 'loading' | 'ready' | 'error'
  nextCursor: string | null
  pages: number
}
interface FilesState {
  projectId: string
  availability: 'loading' | 'ready' | 'uninitialized' | 'paused' | 'unavailable'
  connection: ResourceFeedState
  directories: Record<string, DirectoryState>
  expanded: ReadonlySet<string>
  selected: WorkspaceFile | null
  preview: WorkspacePreview | null
  previewPhase: 'idle' | 'loading' | 'ready' | 'error' | 'missing'
  updated: boolean
}
const emptyDirectory = (): DirectoryState => ({ entries: [], phase: 'loading', nextCursor: null, pages: 1 })
const initialState = (projectId: string): FilesState => ({
  projectId, availability: 'loading', connection: 'connecting', directories: {},
  expanded: new Set(['/']), selected: null, preview: null, previewPhase: 'idle', updated: false,
})
const identity = () => JSON.stringify([getServerAddress(), getAuthorizationHeader()])
const subscribeIdentity = (notify: () => void) => {
  const releaseAuth = subscribeAuthSession(notify)
  const releaseServer = subscribeServerAddress(notify)
  return () => { releaseAuth(); releaseServer() }
}

interface FileActions {
  directory: (path: string, cursor?: string | null) => void
  preview: (file: WorkspaceFile) => void
  collapse: (path: string) => void
}

/** 当前项目拥有查询与通知；关闭后保留阅读状态，项目或身份变化拒绝旧响应 */
export function useWorkspaceFiles(projectId: string, enabled: boolean) {
  const ownerIdentity = useSyncExternalStore(subscribeIdentity, identity)
  const stateIdentity = useRef(ownerIdentity)
  const [state, setState] = useState(() => initialState(projectId))
  const stateRef = useRef(state)
  const [attempt, setAttempt] = useState(0)
  const actions = useRef<FileActions | null>(null)
  const publish = useCallback((change: (previous: FilesState) => FilesState) => {
    const next = change(stateRef.current)
    stateRef.current = next
    setState(next)
  }, [])

  useEffect(() => {
    if (stateRef.current.projectId !== projectId || stateIdentity.current !== ownerIdentity) {
      stateIdentity.current = ownerIdentity
      publish(() => initialState(projectId))
    }
    if (!enabled || !getAuthorizationHeader()) return
    let closed = false
    const current = () => !closed && ownerIdentity === identity() && stateRef.current.projectId === projectId
    const activeDirectories = new Map<string, AbortController>()
    const queuedDirectories = new Map<string, string | null>()
    const refreshAgain = new Set<string>()
    let previewRequest: AbortController | null = null
    let infoRequest: AbortController | null = null
    let changeTimer: ReturnType<typeof setTimeout> | undefined
    let baselineReady = false
    let noticeSequence = 0

    const unavailable = (error: unknown) => {
      if (!current()) return
      const code = error instanceof ApiError ? error.code : undefined
      if (code === WORKSPACE_ERRORS.uninitialized) { abortReads(); publish(previous => ({ ...initialState(projectId), availability: 'uninitialized', connection: previous.connection })) }
      else if (code === WORKSPACE_ERRORS.paused || code === WORKSPACE_ERRORS.forbidden) {
        abortReads()
        publish(previous => ({ ...initialState(projectId), availability: code === WORKSPACE_ERRORS.paused ? 'paused' : 'unavailable', connection: previous.connection }))
      }
      else if (!stateRef.current.directories['/']?.entries.length) publish(previous => ({ ...previous, availability: 'unavailable' }))
    }

    const pump = () => {
      if (!current() || document.hidden || !baselineReady) return
      while (activeDirectories.size < 4 && queuedDirectories.size) {
        const [path, cursor] = queuedDirectories.entries().next().value!
        queuedDirectories.delete(path)
        const controller = new AbortController()
        activeDirectories.set(path, controller)
        const previous = stateRef.current.directories[path] ?? emptyDirectory()
        const pages = cursor ? 1 : previous.pages
        publish(value => ({ ...value, directories: { ...value.directories, [path]: { ...previous, phase: 'loading' } } }))
        const load = async () => {
          let nextCursor = cursor
          let entries: WorkspaceFile[] = []
          let loaded = 0
          try {
            do {
              const page = await readWorkspaceDirectory(projectId, path, nextCursor, controller.signal)
              if (!current() || controller.signal.aborted) return
              if (page.state === 'uninitialized') {
                abortReads()
                publish(value => ({ ...initialState(projectId), availability: 'uninitialized', connection: value.connection }))
                return
              }
              entries = [...entries, ...page.entries]
              nextCursor = page.nextCursor
              loaded += 1
            } while (nextCursor && loaded < pages)
            const combined = cursor ? [...previous.entries, ...entries] : entries
            const unique = [...new Map(combined.map(item => [item.path, item])).values()]
            publish(value => ({ ...value, availability: 'ready', directories: {
              ...value.directories, [path]: { entries: unique, nextCursor, phase: 'ready', pages: cursor ? previous.pages + 1 : loaded },
            } }))
          } catch (error) {
            if (!current() || controller.signal.aborted) return
            if (error instanceof ApiError && error.code === WORKSPACE_ERRORS.changed && cursor) {
              publish(value => ({ ...value, directories: { ...value.directories, [path]: { ...previous, pages: 1 } } }))
              refreshAgain.add(path)
            } else {
              publish(value => ({ ...value, directories: { ...value.directories, [path]: { ...previous, phase: 'error' } } }))
              if (path === '/') unavailable(error)
            }
          } finally {
            if (activeDirectories.get(path) === controller) {
              activeDirectories.delete(path)
              if (current()) {
                if (refreshAgain.delete(path)) queuedDirectories.set(path, null)
                pump()
              }
            }
          }
        }
        void load()
      }
    }

    const directory = (path: string, cursor: string | null = null) => {
      if (!current() || document.hidden) return
      if (activeDirectories.has(path)) { if (!cursor) refreshAgain.add(path); return }
      queuedDirectories.set(path, cursor)
      pump()
    }
    const checkSelected = () => {
      const selected = stateRef.current.selected
      if (!selected || !current() || document.hidden) return
      infoRequest?.abort()
      const controller = new AbortController()
      infoRequest = controller
      void readWorkspaceFileInfo(projectId, selected.path, controller.signal).then(file => {
        if (!current() || controller.signal.aborted || stateRef.current.selected?.path !== selected.path) return
        const content = stateRef.current.preview
        publish(previous => ({ ...previous, selected: file, updated: Boolean(content && content.file.etag !== file.etag) }))
      }).catch(error => {
        if (!current() || controller.signal.aborted) return
        if (error instanceof ApiError && error.code === WORKSPACE_ERRORS.notFound) {
          previewRequest?.abort()
          publish(previous => ({ ...previous, preview: null, previewPhase: 'missing', updated: false }))
        } else unavailable(error)
      }).finally(() => { if (infoRequest === controller) infoRequest = null })
    }
    const reconcile = () => {
      if (!current() || document.hidden) return
      for (const path of stateRef.current.expanded) directory(path)
      checkSelected()
    }
    const preview = (file: WorkspaceFile) => {
      if (!current() || document.hidden) return
      previewRequest?.abort()
      infoRequest?.abort()
      const controller = new AbortController()
      previewRequest = controller
      const startedAtNotice = noticeSequence
      publish(previous => ({ ...previous, selected: file,
        preview: previous.preview?.file.path === file.path ? previous.preview : null,
        previewPhase: 'loading', updated: false,
      }))
      if (!baselineReady) return
      void readWorkspacePreview(projectId, file.path, controller.signal).then(result => {
        if (!current() || controller.signal.aborted || stateRef.current.selected?.path !== file.path) return
        publish(previous => ({ ...previous, selected: result.file, preview: result, previewPhase: 'ready', updated: false }))
        if (startedAtNotice !== noticeSequence) checkSelected()
      }).catch(error => {
        if (!current() || controller.signal.aborted) return
        publish(previous => ({ ...previous, previewPhase: error instanceof ApiError && error.code === WORKSPACE_ERRORS.notFound ? 'missing' : 'error',
          preview: error instanceof ApiError && error.code === WORKSPACE_ERRORS.notFound ? null : previous.preview,
          updated: error instanceof ApiError && error.code === WORKSPACE_ERRORS.changed,
        }))
        if (error instanceof ApiError && [WORKSPACE_ERRORS.uninitialized, WORKSPACE_ERRORS.paused, WORKSPACE_ERRORS.forbidden].some(code => code === error.code)) unavailable(error)
      }).finally(() => { if (previewRequest === controller) previewRequest = null })
    }

    const abortReads = () => {
      for (const request of activeDirectories.values()) request.abort()
      activeDirectories.clear()
      queuedDirectories.clear()
      refreshAgain.clear()
      previewRequest?.abort()
      infoRequest?.abort()
      clearTimeout(changeTimer)
      changeTimer = undefined
    }
    actions.current = { directory, preview, collapse(path) {
      for (const [key, request] of activeDirectories) {
        if (key === path || key.startsWith(`${path}/`)) { request.abort(); activeDirectories.delete(key) }
      }
      for (const key of queuedDirectories.keys()) if (key === path || key.startsWith(`${path}/`)) queuedDirectories.delete(key)
      for (const key of refreshAgain) if (key === path || key.startsWith(`${path}/`)) refreshAgain.delete(key)
      pump()
    } }
    const closeFeed = startResourceFeed({
      path: `${workspaceEndpoint(projectId)}/events`,
      onFrame(frame) {
        if (!current()) return
        if (frame.event === 'ready' || frame.event === 'resync') {
          noticeSequence += 1
          baselineReady = true
          reconcile()
          const value = stateRef.current
          if (value.selected && (!value.preview || value.previewPhase === 'loading')) preview(value.selected)
        } else if (frame.event === 'change') {
          if (!frame.data || typeof frame.data !== 'object' || !('kind' in frame.data) || frame.data.kind !== 'files_changed') throw new SseError('stream_data_invalid')
          noticeSequence += 1
          changeTimer ??= setTimeout(() => { changeTimer = undefined; reconcile() }, 150)
        }
      },
      onState(connection, error) {
        if (!current()) return
        publish(previous => ({ ...previous, connection }))
        if (connection === 'hidden') { baselineReady = false; abortReads() }
        if (connection === 'disconnected') { baselineReady = false; abortReads(); unavailable(error) }
      },
    })
    const calibration = setInterval(() => { if (baselineReady) reconcile() }, 30_000)
    return () => {
      closed = true
      closeFeed()
      abortReads()
      clearInterval(calibration)
      actions.current = null
    }
  }, [projectId, enabled, attempt, ownerIdentity, publish])

  const toggleDirectory = useCallback((path: string) => {
    const expanded = new Set(stateRef.current.expanded)
    const closing = expanded.has(path)
    if (closing) {
      for (const key of expanded) if (key === path || key.startsWith(`${path}/`)) expanded.delete(key)
      actions.current?.collapse(path)
    } else expanded.add(path)
    publish(previous => ({ ...previous, expanded }))
    if (!closing) actions.current?.directory(path)
  }, [publish])
  const selectFile = useCallback((file: WorkspaceFile) => actions.current?.preview(file), [])
  const retryDirectory = useCallback((path: string) => actions.current?.directory(path), [])
  const loadMore = useCallback((path: string) => {
    const cursor = stateRef.current.directories[path]?.nextCursor
    if (cursor) actions.current?.directory(path, cursor)
  }, [])
  const refreshPreview = useCallback(() => { if (stateRef.current.selected) actions.current?.preview(stateRef.current.selected) }, [])
  const refresh = useCallback(() => setAttempt(value => value + 1), [])
  return { ...(state.projectId === projectId && stateIdentity.current === ownerIdentity ? state : initialState(projectId)), toggleDirectory, selectFile, retryDirectory, loadMore, refreshPreview, refresh }
}

export type WorkspaceFilesState = ReturnType<typeof useWorkspaceFiles>
