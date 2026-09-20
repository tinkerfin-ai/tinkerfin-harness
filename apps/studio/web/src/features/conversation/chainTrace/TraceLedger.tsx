import { ChevronDown } from 'lucide-react'
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { OverlayScrollbar } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { TraceTurnRows } from './traceLayout'
import {
  durationLabel,
  traceNodeAccessibleLabel,
  traceStatusLabel,
} from './tracePresentation'
import {
  TraceNodeCopy,
  TraceNodeMeta,
  TraceNodeType,
} from './TraceNodeVisual'

interface ScopeRowStyle extends CSSProperties {
  '--chain-trace-scope-depth': number
}

const traceLedgerRowId = (nodeId: string) => (
  `trace-ledger-node-${encodeURIComponent(nodeId)}`
)

export function TraceLedger({
  groups,
  directNodeIds,
  selectedId,
  onSelect,
  onHideSelection,
}: {
  groups: TraceTurnRows[]
  directNodeIds: ReadonlySet<string>
  selectedId?: string
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
  onHideSelection: (fallback: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const scrollRef = useRef<HTMLDivElement>(null)
  const headerViewportRef = useRef<HTMLDivElement>(null)
  const [collapsedTurnIds, setCollapsedTurnIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const [collapsedSubagentIds, setCollapsedSubagentIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const nodesById = useMemo(
    () => new Map(groups.flatMap(({ nodes }) => nodes).map((node) => [node.id, node])),
    [groups],
  )
  const expandableSubagentIds = useMemo(
    () => new Set(
      groups.flatMap(({ nodes }) => nodes)
        .flatMap((node) => node.parentSubagentId ? [node.parentSubagentId] : []),
    ),
    [groups],
  )
  const depths = useMemo(() => {
    const values = new Map<string, number>()
    groups.flatMap(({ nodes }) => nodes).forEach((node) => {
      const parentDepth = node.parentSubagentId
        ? values.get(node.parentSubagentId)
        : -1
      if (node.parentSubagentId && parentDepth === undefined) {
        throw new Error('invalid Subagent scope order')
      }
      values.set(node.id, (parentDepth ?? -1) + 1)
    })
    return values
  }, [groups])
  useLayoutEffect(() => {
    const selected = selectedId ? nodesById.get(selectedId) : undefined
    if (!selected) return
    setCollapsedTurnIds((current) => {
      if (!current.has(selected.turnId)) return current
      const next = new Set(current)
      next.delete(selected.turnId)
      return next
    })
    const owners = new Set<string>()
    let ownerId = selected.parentSubagentId
    while (ownerId) {
      owners.add(ownerId)
      ownerId = nodesById.get(ownerId)?.parentSubagentId
    }
    setCollapsedSubagentIds((current) => {
      if (![...owners].some((id) => current.has(id))) return current
      return new Set([...current].filter((id) => !owners.has(id)))
    })
  }, [nodesById, selectedId])
  const hiddenNodeIds = useMemo(() => {
    const hidden = new Set<string>()
    groups.flatMap(({ nodes }) => nodes).forEach((node) => {
      const ownerId = node.parentSubagentId
      if (ownerId && (collapsedSubagentIds.has(ownerId) || hidden.has(ownerId))) {
        hidden.add(node.id)
      }
    })
    return hidden
  }, [collapsedSubagentIds, groups])
  const controlledRowIds = useMemo(() => {
    const descendants = new Map<string, string[]>()
    const ancestors = new Map<string, string[]>()
    groups.flatMap(({ nodes }) => nodes).forEach((node) => {
      const parentAncestors = node.parentSubagentId
        ? ancestors.get(node.parentSubagentId) ?? []
        : []
      const nodeAncestors = node.parentSubagentId
        ? [...parentAncestors, node.parentSubagentId]
        : []
      ancestors.set(node.id, nodeAncestors)
      nodeAncestors.forEach((subagentId) => descendants.set(subagentId, [
        ...(descendants.get(subagentId) ?? []),
        traceLedgerRowId(node.id),
      ]))
    })
    return descendants
  }, [groups])

  const toggleTurn = (turnId: string, trigger: HTMLButtonElement) => {
    if (
      !collapsedTurnIds.has(turnId)
      && selectedId
      && nodesById.get(selectedId)?.turnId === turnId
    ) onHideSelection(trigger)
    setCollapsedTurnIds((current) => {
      const next = new Set(current)
      if (next.has(turnId)) next.delete(turnId)
      else next.add(turnId)
      return next
    })
  }
  const toggleSubagent = (nodeId: string, trigger: HTMLButtonElement) => {
    if (!collapsedSubagentIds.has(nodeId) && selectedId) {
      let ownerId = nodesById.get(selectedId)?.parentSubagentId
      while (ownerId) {
        if (ownerId === nodeId) {
          onHideSelection(trigger)
          break
        }
        ownerId = nodesById.get(ownerId)?.parentSubagentId
      }
    }
    setCollapsedSubagentIds((current) => {
      const next = new Set(current)
      if (next.has(nodeId)) next.delete(nodeId)
      else next.add(nodeId)
      return next
    })
  }
  return (
    <div className="chain-trace-ledger-host">
      <div
        ref={headerViewportRef}
        className="chain-trace-ledger-header-viewport"
        aria-hidden="true"
      >
        <div className="chain-trace-ledger-header">
          <span>{t('节点')}</span>
          <span>{t('类型')}</span>
          <span>{t('节点与内容预览')}</span>
          <span>{t('耗时')}</span>
        </div>
      </div>
      <div className="chain-trace-ledger-scroll-host">
        <div
          ref={scrollRef}
          className="chain-trace-ledger"
          aria-label={t('链路节点')}
          tabIndex={-1}
          onScroll={(event) => {
            const headerViewport = headerViewportRef.current
            if (headerViewport) headerViewport.scrollLeft = event.currentTarget.scrollLeft
          }}
        >
          {groups.map(({ turn, nodes }) => {
            const collapsed = collapsedTurnIds.has(turn.id)
            const contentId = `trace-ledger-${turn.id}`
            const rootNodes = nodes.filter((node) => node.parentSubagentId == null)
            const firstRootId = rootNodes[0]?.id
            const lastRootId = rootNodes.at(-1)?.id
            const startedAt = Date.parse(turn.startedAt)
            const completedAt = nodes.reduce((latest, node) => Math.max(
              latest,
              Date.parse(node.completedAt ?? node.firstOutputAt ?? node.startedAt),
            ), startedAt)
            return (
              <section
                key={turn.id}
                className="chain-trace-ledger-turn"
                aria-label={t('第 {count} 轮', { count: turn.ordinal })}
              >
                <button
                  type="button"
                  className="chain-trace-turn-heading"
                  aria-expanded={!collapsed}
                  aria-controls={contentId}
                  onClick={(event) => toggleTurn(turn.id, event.currentTarget)}
                >
                  <ChevronDown size={14} aria-hidden="true" />
                  <strong>{t('第 {count} 轮', { count: turn.ordinal })}</strong>
                  <span className="chain-trace-turn-duration">
                    {durationLabel(Math.max(0, completedAt - startedAt), t)}
                  </span>
                </button>
                {!collapsed && (
                  <div id={contentId} className="chain-trace-turn-rows">
                    {nodes.map((node, index) => (
                      <TraceLedgerRow
                        key={node.id}
                        node={node}
                        depth={depths.get(node.id) ?? 0}
                        direct={directNodeIds.has(node.id)}
                        selected={node.id === selectedId}
                        expandable={expandableSubagentIds.has(node.id)}
                        collapsed={collapsedSubagentIds.has(node.id)}
                        turnOrdinal={turn.ordinal}
                        stepOrdinal={index + 1}
                        hidden={hiddenNodeIds.has(node.id)}
                        controlledRowIds={controlledRowIds.get(node.id) ?? []}
                        firstTurnRoot={node.id === firstRootId}
                        lastTurnRoot={node.id === lastRootId}
                        onToggleSubagent={toggleSubagent}
                        onSelect={onSelect}
                      />
                    ))}
                  </div>
                )}
              </section>
            )
          })}
        </div>
        <OverlayScrollbar viewportRef={scrollRef} />
      </div>
    </div>
  )
}

function TraceLedgerRow({
  node,
  depth,
  direct,
  selected,
  expandable,
  collapsed,
  turnOrdinal,
  stepOrdinal,
  hidden,
  controlledRowIds,
  firstTurnRoot,
  lastTurnRoot,
  onToggleSubagent,
  onSelect,
}: {
  node: TraceGraphNode
  depth: number
  direct: boolean
  selected: boolean
  expandable: boolean
  collapsed: boolean
  turnOrdinal: number
  stepOrdinal: number
  hidden: boolean
  controlledRowIds: string[]
  firstTurnRoot: boolean
  lastTurnRoot: boolean
  onToggleSubagent: (nodeId: string, trigger: HTMLButtonElement) => void
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
}) {
  const { t } = useI18n()
  const subagent = node.kind === 'subagent'
  return (
    <div
      id={traceLedgerRowId(node.id)}
      className={`chain-trace-ledger-row-shell${direct ? '' : ' is-structural'}`}
      style={{ '--chain-trace-scope-depth': depth } as ScopeRowStyle}
      hidden={hidden}
    >
      {subagent && expandable && (
        <button
          type="button"
          className="chain-trace-ledger-toggle"
          aria-label={collapsed
            ? t('展开子智能体 {name}，第 {turn} 轮步骤 {step}', {
                name: node.name,
                turn: turnOrdinal,
                step: stepOrdinal,
              })
            : t('收起子智能体 {name}，第 {turn} 轮步骤 {step}', {
                name: node.name,
                turn: turnOrdinal,
                step: stepOrdinal,
              })}
          aria-expanded={!collapsed}
          aria-controls={controlledRowIds.join(' ')}
          onClick={(event) => onToggleSubagent(node.id, event.currentTarget)}
        >
          <ChevronDown size={14} aria-hidden="true" />
        </button>
      )}
      <button
        type="button"
        className={`chain-trace-ledger-row${selected ? ' is-selected' : ''}${node.failure ? ' has-error' : ''}${firstTurnRoot ? ' is-turn-root-start' : ''}${lastTurnRoot ? ' is-turn-root-end' : ''}`}
        data-trace-node-id={node.id}
        aria-label={`${direct ? '' : `${t('范围')}，`}${traceNodeAccessibleLabel(node, t)}，${traceStatusLabel(node.status, t)}，${t('查看详情')}`}
        aria-current={selected || undefined}
        onClick={(event) => onSelect(node.id, event.currentTarget)}
      >
        <span className="chain-trace-ledger-rail"><i /></span>
        <TraceNodeType node={node} />
        <span className="chain-trace-ledger-main">
          {!direct && <span className="chain-trace-scope-badge">{t('范围')}</span>}
          <TraceNodeCopy
            node={node}
            showKind={false}
          />
        </span>
        <TraceNodeMeta node={node} />
      </button>
    </div>
  )
}
