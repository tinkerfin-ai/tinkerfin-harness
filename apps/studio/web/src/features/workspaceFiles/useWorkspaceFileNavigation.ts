import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { restoreFocus } from '../../components/ui/focus'
import type { WorkspaceFile } from './api'
import type { WorkspaceFilesState } from './useWorkspaceFiles'
import type { WorkspaceFileView } from './WorkspaceFileBrowser'

/** 目录、视图和源码各自保留滚动位置，返回目录恢复原文件焦点 */
export function useWorkspaceFileNavigation(state: WorkspaceFilesState, open: boolean) {
  const [view, setView] = useState<WorkspaceFileView>('list')
  const content = useRef<HTMLDivElement>(null)
  const viewport = useRef<HTMLDivElement>(null)
  const backButton = useRef<HTMLButtonElement>(null)
  const positions = useRef(new Map<string, { top: number; left: number }>())
  const returnPath = useRef<string | undefined>(undefined)
  const selectedPath = state.selected?.path
  const key = `${state.projectId}:${state.directoryPath}:${selectedPath ?? view}`
  const directory = state.directories[state.directoryPath]
  const ready = state.availability === 'ready' && (selectedPath ? Boolean(state.preview) : Boolean(directory?.entries.length || directory?.phase === 'ready'))
  const priorKey = useRef(key)
  const priorDirectory = useRef(state.directoryPath)
  const wasOpen = useRef(false)
  const pendingRestore = useRef<string | undefined>(key)
  const onScroll = useCallback(() => {
    const element = viewport.current
    if (element && open && ready && pendingRestore.current !== key) positions.current.set(key, { top: Math.max(0, element.scrollTop), left: Math.max(0, element.scrollLeft) })
  }, [key, open, ready])
  useLayoutEffect(() => {
    if (!open) { wasOpen.current = false; return }
    if (priorKey.current !== key || !wasOpen.current) pendingRestore.current = key
    wasOpen.current = true
    const element = viewport.current
    // 占位内容没有可恢复的滚动范围；正文到达后再恢复，期间不覆盖已存位置
    if (element && ready && pendingRestore.current === key) {
      const position = positions.current.get(key)
      element.scrollTop = position?.top ?? 0
      element.scrollLeft = position?.left ?? 0
      pendingRestore.current = undefined
    }
    if (priorKey.current !== key) {
      if (selectedPath) restoreFocus(backButton.current, { preventScroll: true })
      else if (returnPath.current) {
        const target = Array.from(element?.querySelectorAll<HTMLButtonElement>('[data-file-path]') ?? []).find(item => item.dataset.filePath === returnPath.current)
        restoreFocus(target ?? element, { preventScroll: true })
        returnPath.current = undefined
      } else if (priorDirectory.current !== state.directoryPath) restoreFocus(element, { preventScroll: true })
    }
    priorKey.current = key
    priorDirectory.current = state.directoryPath
  }, [key, open, selectedPath, state.directoryPath, ready])
  const openDirectory = (path: string) => { onScroll(); state.openDirectory(path) }
  const openFile = (file: WorkspaceFile) => { onScroll(); state.selectFile(file) }
  const { closeFile: returnToDirectory } = state
  const closeFile = useCallback(() => { onScroll(); returnPath.current = selectedPath; returnToDirectory() }, [onScroll, selectedPath, returnToDirectory])
  useEffect(() => {
    const element = content.current
    if (!open || !selectedPath || !element) return
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      event.stopPropagation()
      closeFile()
    }
    element.addEventListener('keydown', escape)
    return () => element.removeEventListener('keydown', escape)
  }, [open, selectedPath, closeFile])
  const changeView = (next: WorkspaceFileView) => { onScroll(); setView(next) }
  return { view, content, viewport, backButton, onScroll, openDirectory, openFile, closeFile, changeView }
}
