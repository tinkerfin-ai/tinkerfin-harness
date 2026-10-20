import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { RefObject } from 'react'

import type { TodoGroup } from '../../../api/conversation/taskTrace'

const COLLAPSED_ROW_HEIGHT = 72
const TODO_ROW_HEIGHT = 36
const SECTION_LABEL_HEIGHT = 32
const DEFAULT_OVERSCAN = 12

export interface TodoGroupWindowItem {
  group: TodoGroup
  index: number
  top: number
  height: number
}

export interface TodoGroupWindowResult {
  items: TodoGroupWindowItem[]
  offsets: number[]
  totalHeight: number
}

const estimatedHeight = (
  group: TodoGroup,
  expanded: boolean,
  hasSectionLabel: boolean,
) => (
  COLLAPSED_ROW_HEIGHT
  + (hasSectionLabel ? SECTION_LABEL_HEIGHT : 0)
  + (expanded ? Math.max(TODO_ROW_HEIGHT, group.todos.length * TODO_ROW_HEIGHT + 60) : 0)
)

const firstRowAfter = (offsets: number[], value: number) => {
  let low = 0
  let high = offsets.length - 1
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if ((offsets[middle] ?? 0) < value) low = middle + 1
    else high = middle
  }
  return low
}

export const calculateTodoGroupWindow = ({
  groups,
  expandedIds = new Set(),
  scrollTop,
  viewportHeight,
  measuredHeights = new Map(),
  overscan = DEFAULT_OVERSCAN,
}: {
  groups: readonly TodoGroup[]
  expandedIds?: ReadonlySet<string>
  scrollTop: number
  viewportHeight: number
  measuredHeights?: ReadonlyMap<string, number>
  overscan?: number
}): TodoGroupWindowResult => {
  const offsets = new Array<number>(groups.length + 1).fill(0)
  const hasCurrentGroup = groups[0]?.status === 'running'
  for (let index = 0; index < groups.length; index += 1) {
    const group = groups[index]
    if (!group) continue
    const expanded = expandedIds.has(group.id)
    const height = (expanded ? measuredHeights.get(group.id) : undefined)
      ?? estimatedHeight(
        group,
        expanded,
        index === 0 || (hasCurrentGroup && index === 1),
      )
    offsets[index + 1] = (offsets[index] ?? 0) + height
  }
  if (groups.length === 0) return { items: [], offsets, totalHeight: 0 }
  const firstVisible = Math.max(0, firstRowAfter(offsets, scrollTop) - 1)
  const lastVisible = Math.min(
    groups.length - 1,
    firstRowAfter(offsets, scrollTop + Math.max(1, viewportHeight)),
  )
  const start = Math.max(0, firstVisible - overscan)
  const end = Math.min(groups.length - 1, lastVisible + overscan)
  const items: TodoGroupWindowItem[] = []
  for (let index = start; index <= end; index += 1) {
    const group = groups[index]
    if (!group) continue
    items.push({
      group,
      index,
      top: offsets[index] ?? 0,
      height: (offsets[index + 1] ?? 0) - (offsets[index] ?? 0),
    })
  }
  return {
    items,
    offsets,
    totalHeight: offsets.at(-1) ?? 0,
  }
}

export function useTodoGroupWindow({
  groups,
  expandedIds,
  viewportRef,
}: {
  groups: readonly TodoGroup[]
  expandedIds: ReadonlySet<string>
  viewportRef: RefObject<HTMLElement | null>
}) {
  const measuredHeights = useRef(new Map<string, number>())
  const observers = useRef(new Map<string, ResizeObserver>())
  const anchor = useRef<{ id: string; offset: number } | undefined>(undefined)
  const previousIds = useRef<string[]>([])
  const previouslyExpandedIds = useRef<ReadonlySet<string>>(new Set())
  const [measurementVersion, setMeasurementVersion] = useState(0)
  const [viewport, setViewport] = useState({ scrollTop: 0, height: 720 })
  const previousExpandedIds = previouslyExpandedIds.current
  const changedExpansionIds = new Set([...previousExpandedIds, ...expandedIds])
  for (const id of changedExpansionIds) {
    if (previousExpandedIds.has(id) !== expandedIds.has(id)) measuredHeights.current.delete(id)
  }
  previouslyExpandedIds.current = new Set(expandedIds)

  useEffect(() => {
    const element = viewportRef.current
    if (!element) return
    let frame: number | null = null
    const measure = () => {
      frame = null
      setViewport({ scrollTop: element.scrollTop, height: element.clientHeight || 720 })
    }
    const schedule = () => {
      if (frame == null) frame = window.requestAnimationFrame(measure)
    }
    const resize = new ResizeObserver(schedule)
    resize.observe(element)
    element.addEventListener('scroll', schedule, { passive: true })
    measure()
    return () => {
      if (frame != null) window.cancelAnimationFrame(frame)
      resize.disconnect()
      element.removeEventListener('scroll', schedule)
    }
  }, [viewportRef])

  const windowResult = useMemo(() => {
    void measurementVersion
    return calculateTodoGroupWindow({
      groups,
      expandedIds,
      scrollTop: viewport.scrollTop,
      viewportHeight: viewport.height,
      measuredHeights: measuredHeights.current,
    })
  }, [expandedIds, groups, measurementVersion, viewport.height, viewport.scrollTop])

  useLayoutEffect(() => {
    const element = viewportRef.current
    const ids = groups.map((group) => group.id)
    const previous = previousIds.current
    previousIds.current = ids
    if (!element || previous.length === 0 || previous[0] === ids[0]) return
    const retained = anchor.current
    if (!retained) return
    const index = ids.indexOf(retained.id)
    if (index < 0) return
    element.scrollTop = Math.max(
      0,
      (windowResult.offsets[index] ?? 0) + retained.offset,
    )
  }, [groups, viewportRef, windowResult.offsets])

  useEffect(() => {
    const first = windowResult.items.find((item) => item.top + item.height > viewport.scrollTop)
    if (!first) return
    anchor.current = {
      id: first.group.id,
      offset: viewport.scrollTop - first.top,
    }
  }, [viewport.scrollTop, windowResult.items])

  useEffect(() => () => {
    for (const observer of observers.current.values()) observer.disconnect()
    observers.current.clear()
  }, [])

  const registerRow = useCallback((groupId: string, element: HTMLElement | null) => {
    observers.current.get(groupId)?.disconnect()
    observers.current.delete(groupId)
    if (!element) return
    const update = () => {
      const height = element.getBoundingClientRect().height
      if (height <= 0 || measuredHeights.current.get(groupId) === height) return
      measuredHeights.current.set(groupId, height)
      setMeasurementVersion((value) => value + 1)
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    observers.current.set(groupId, observer)
  }, [])

  const scrollToIndex = useCallback((index: number) => {
    const element = viewportRef.current
    if (!element || index < 0 || index >= groups.length) return
    const top = windowResult.offsets[index] ?? 0
    const bottom = windowResult.offsets[index + 1] ?? top
    if (top < element.scrollTop) element.scrollTop = top
    else if (bottom > element.scrollTop + element.clientHeight) {
      element.scrollTop = Math.max(0, bottom - element.clientHeight)
    }
  }, [groups.length, viewportRef, windowResult.offsets])

  return { ...windowResult, registerRow, scrollToIndex }
}
