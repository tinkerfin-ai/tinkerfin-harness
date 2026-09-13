import { Activity, ChevronRight, Search, ShieldAlert } from 'lucide-react'
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { createPortal } from 'react-dom'

import type {
  TraceGraphNode,
} from '../../../api/conversation/traceGraph'
import { compareTraceGraphNodes } from '../../../api/conversation/traceGraph'
import {
  Button,
  DrawerHeader,
  FeedbackState,
  IconButton,
  OverlayScrollbar,
  SearchField,
  ViewTabs,
} from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { JsonObject, JsonValue } from '../../../types'
import { MarkdownContent } from '../components/MarkdownContent'
import { TraceLedger } from './TraceLedger'
import { TraceNodeType } from './TraceNodeVisual'
import { TraceTimeline } from './TraceTimeline'
import {
  buildTraceSequenceLayout,
  buildTraceTimelineLayout,
  groupTraceNodesByTurn,
  preferredTraceNode,
} from './traceLayout'
import {
  durationLabel,
  elapsedMilliseconds,
  traceContentText,
  traceKindLabel,
  traceStatusLabel,
} from './tracePresentation'
import { useChainTrace } from './useChainTrace'
import { useTraceModelResponse } from './useTraceModelResponse'

const TRACE_SEARCH_DELAY_MS = 250
const DETAILS_INLINE_MIN_WIDTH = 920
const FOCUSABLE = [
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

type DetailTab = 'overview' | 'request' | 'system' | 'response' | 'usage' | 'timing' | 'result'

const json = (value: unknown) => value == null ? '' : JSON.stringify(value, null, 2)

const isJsonObject = (value: unknown): value is JsonObject => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

interface UsageRow {
  key: string
  path: string[]
  value: string | number | boolean | null
}

const flattenUsage = (
  value: JsonValue,
  path: string[] = [],
): UsageRow[] => {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => flattenUsage(item, [...path, String(index + 1)]))
  }
  if (isJsonObject(value)) {
    return Object.entries(value).flatMap(([name, item]) => (
      flattenUsage(item, [...path, name])
    ))
  }
  return [{ key: path.join('.'), path, value }]
}

const mergeTraceNodes = (
  ...groups: ReadonlyArray<readonly TraceGraphNode[]>
) => {
  const byId = new Map<string, TraceGraphNode>()
  groups.flat().forEach((node) => {
    const current = byId.get(node.id)
    if (!current || node.updatedSeq >= current.updatedSeq) byId.set(node.id, node)
  })
  return [...byId.values()].sort(compareTraceGraphNodes)
}

const findTraceNodeTrigger = (
  region: HTMLElement | null,
  nodeId: string | undefined,
) => {
  if (!nodeId) return undefined
  return [...(region?.querySelectorAll<HTMLButtonElement>('[data-trace-node-id]') ?? [])]
    .find((element) => element.dataset.traceNodeId === nodeId)
}

const restoreTraceNodeFocus = (
  region: HTMLElement | null,
  nodeId: string | undefined,
  trigger: HTMLButtonElement | null,
) => {
  window.requestAnimationFrame(() => {
    const target = trigger?.isConnected
      ? trigger
      : findTraceNodeTrigger(region, nodeId)
    target?.focus()
  })
}

const systemPrompt = (entry: TraceGraphNode) => {
  if (!entry.request || typeof entry.request !== 'object' || Array.isArray(entry.request)) return ''
  const messages = entry.request.messages
  if (!Array.isArray(messages)) return ''
  return messages
    .filter(isJsonObject)
    .filter((message) => message.messageType === 'system')
    .map((message) => traceContentText(message.content))
    .filter(Boolean)
    .join('\n\n')
}

const useTraceDetailsOverlay = (
  containerRef: RefObject<HTMLDivElement | null>,
  enabled: boolean,
) => {
  const [overlay, setOverlay] = useState<boolean | undefined>()
  useLayoutEffect(() => {
    if (!enabled) {
      setOverlay(undefined)
      return undefined
    }
    const container = containerRef.current
    if (!container) return undefined
    const measure = () => {
      if (container.clientWidth > 0) {
        setOverlay(container.clientWidth <= DETAILS_INLINE_MIN_WIDTH)
      }
    }
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(container)
    return () => observer.disconnect()
  }, [containerRef, enabled])
  return overlay
}

function TraceDetails({
  entry,
  responseEntries,
  responseStatus,
  turnOrdinal,
  stepOrdinal,
  overlay,
  onRetryResponse,
  onClose,
}: {
  entry: TraceGraphNode
  responseEntries: TraceGraphNode[]
  responseStatus: 'loading' | 'ready' | 'error'
  turnOrdinal?: number
  stepOrdinal?: number
  overlay: boolean
  onRetryResponse: () => void
  onClose: () => void
}) {
  const { locale, t } = useI18n()
  const [tab, setTab] = useState<DetailTab>('overview')
  const closeButton = useRef<HTMLButtonElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)
  const prompt = systemPrompt(entry)
  const responseMessages = responseEntries
    .filter((item) => item.kind === 'assistant_message')
    .map((item) => ({
      id: item.sourceId ?? item.id,
      type: 'AIMessage',
      content: traceContentText(item.content),
    }))
  const responseTools = responseEntries
    .filter((item) => item.kind === 'tool' || item.kind === 'subagent')
    .map((item) => ({
      id: item.sourceId ?? item.id,
      name: item.name,
      arguments: item.request ?? null,
    }))
  const responseData = {
    messages: responseMessages.map(({ id, type }) => ({ id, type })),
    toolCalls: responseTools,
    usageMetadata: entry.usage ?? null,
    responseMetadata: entry.responseMetadata ?? null,
  }
  const messageContent = entry.kind.endsWith('_message') || entry.kind === 'context'
    ? traceContentText(entry.content)
    : ''
  const usageRows = entry.usage == null ? [] : flattenUsage(entry.usage)
  const usageLabels: Record<string, string> = {
    input_tokens: t('输入 Tokens'),
    output_tokens: t('输出 Tokens'),
    total_tokens: t('总 Tokens'),
    'input_token_details.cache_read': t('缓存读取 Tokens'),
    'input_token_details.cache_creation': t('缓存创建 Tokens'),
    'input_token_details.audio': t('输入音频 Tokens'),
    'output_token_details.reasoning': t('推理 Tokens'),
    'output_token_details.audio': t('输出音频 Tokens'),
    'output_token_details.accepted_prediction': t('接受预测 Tokens'),
    'output_token_details.rejected_prediction': t('拒绝预测 Tokens'),
  }
  const tabs: Array<{ id: DetailTab; label: string }> = [
    { id: 'overview', label: t('概述') },
    ...(entry.request != null || entry.requestOmitted
      ? [{ id: 'request' as const, label: t('请求') }]
      : []),
    ...(prompt ? [{ id: 'system' as const, label: t('系统提示词') }] : []),
    ...(entry.kind === 'model'
      ? [{ id: 'response' as const, label: t('响应') }]
      : []),
    ...(usageRows.length > 0 ? [{ id: 'usage' as const, label: t('用量') }] : []),
    { id: 'timing', label: t('计时') },
    ...(entry.result != null || entry.resultOmitted || entry.failure
      ? [{ id: 'result' as const, label: t('结果') }]
      : []),
  ]
  const activeTab = tabs.some((item) => item.id === tab) ? tab : 'overview'

  useLayoutEffect(() => {
    if (!overlay) return undefined
    const overlayElement = overlayRef.current
    if (!overlayElement) return undefined
    const background = Array.from(document.body.children)
      .filter((element): element is HTMLElement => (
        element instanceof HTMLElement && element !== overlayElement
      ))
      .map((element) => ({
        element,
        inert: element.inert,
        ariaHidden: element.getAttribute('aria-hidden'),
      }))
    background.forEach(({ element }) => {
      element.inert = true
      element.setAttribute('aria-hidden', 'true')
    })
    closeButton.current?.focus()
    return () => background.forEach(({ element, inert, ariaHidden }) => {
      element.inert = inert
      if (ariaHidden == null) element.removeAttribute('aria-hidden')
      else element.setAttribute('aria-hidden', ariaHidden)
    })
  }, [overlay])

  const trapFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (!overlay || event.defaultPrevented || event.key !== 'Tab') return
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
    )
    if (focusable.length === 0) {
      event.preventDefault()
      dialogRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && (
      document.activeElement === first
      || !dialogRef.current?.contains(document.activeElement)
    )) {
      event.preventDefault()
      last?.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first?.focus()
    }
  }

  const total = elapsedMilliseconds(entry)
  const ttft = entry.firstOutputAt
    ? Math.max(0, Date.parse(entry.firstOutputAt) - Date.parse(entry.startedAt))
    : null
  const positionLabel = turnOrdinal != null && stepOrdinal != null
    ? t('第 {turn} 轮 · 步骤 {step}', { turn: turnOrdinal, step: stepOrdinal })
    : durationLabel(total, t)

  const detailsBody = (
    <>
      <DrawerHeader
        ref={closeButton}
        density="compact"
        className="chain-trace-details-header"
        title={(
          <span className="chain-trace-details-context">
            <TraceNodeType node={entry} />
            <span>{positionLabel}</span>
          </span>
        )}
        closeLabel={t('关闭链路详情')}
        onClose={onClose}
      />
      <ViewTabs
        value={activeTab}
        options={tabs.map((item) => ({
          value: item.id,
          label: item.label,
          controls: 'chain-trace-detail-panel',
        }))}
        label={t('链路详情分类')}
        density="medium"
        className="chain-trace-detail-tabs"
        onChange={setTab}
      />
      <div className="chain-trace-detail-body-host">
        <div
          ref={bodyRef}
          id="chain-trace-detail-panel"
          className="chain-trace-detail-body"
          role="tabpanel"
          aria-label={tabs.find((item) => item.id === activeTab)?.label}
        >
          {activeTab === 'overview' && (
            <>
              {entry.failure && (
                <section className="chain-trace-error-panel" aria-label={t('错误详情')}>
                  <ShieldAlert size={20} aria-hidden="true" />
                  <div>
                    <strong>{entry.failure.errorType}</strong>
                    {entry.failure.message && <p>{entry.failure.message}</p>}
                  </div>
                  {(entry.request != null || entry.requestOmitted) && (
                    <Button className="chain-trace-feedback-action" size="xs" variant="text" trailingIcon={<ChevronRight size={16} />} onClick={() => setTab('request')}>{t('查看请求')}</Button>
                  )}
                </section>
              )}
              {messageContent && (
                <div className="chain-trace-detail-message">
                  <MarkdownContent content={messageContent} variant="compact" />
                </div>
              )}
              {entry.kind === 'context' && !messageContent && (
                <div className="chain-trace-detail-message is-empty">
                  <p>{entry.contentOmitted
                    ? t('最终系统提示词未保留')
                    : t('本次模型请求没有最终系统提示词')}</p>
                </div>
              )}
              <h3 className="chain-trace-section-title">{t('基本信息')}</h3>
              <dl className="chain-trace-summary">
                <div><dt>{t('状态')}</dt><dd>{traceStatusLabel(entry.status, t)}</dd></div>
                <div><dt>{t('类型')}</dt><dd>{traceKindLabel(entry.kind, t)}</dd></div>
                <div><dt>{t('节点 ID')}</dt><dd>{entry.id}</dd></div>
                <div><dt>{t('运行 ID')}</dt><dd>{entry.runId}</dd></div>
                <div><dt>{t('图范围')}</dt><dd>{entry.graphNamespace.length ? entry.graphNamespace.join(' / ') : t('根图')}</dd></div>
                {entry.provider && <div><dt>{t('提供方')}</dt><dd>{entry.provider}</dd></div>}
                {entry.model && <div><dt>{t('模型')}</dt><dd>{entry.model}</dd></div>}
                {entry.kind === 'tool' && <div><dt>{t('工具')}</dt><dd>{entry.name}</dd></div>}
                {entry.agentName && <div><dt>{t('智能体')}</dt><dd>{entry.agentName}</dd></div>}
                {entry.sourceId && <div><dt>{t('来源 ID')}</dt><dd>{entry.sourceId}</dd></div>}
              </dl>
            </>
          )}
          {activeTab === 'request' && <pre>{entry.requestOmitted ? t('请求内容未保留') : json(entry.request)}</pre>}
          {activeTab === 'system' && <MarkdownContent content={prompt} variant="compact" />}
          {activeTab === 'response' && (
            <div className="chain-trace-response">
              {responseStatus === 'loading' ? (
                <FeedbackState kind="loading" title={t('正在加载完整响应…')} />
              ) : responseStatus === 'error' ? (
                <Button type="button" variant="text" onClick={onRetryResponse}>{t('重新加载')}</Button>
              ) : (
                <>
                  {responseMessages.map((message) => message.content && (
                    <section key={message.id} className="chain-trace-response-message">
                      <span>{message.type} · {message.id}</span>
                      <MarkdownContent content={message.content} variant="compact" />
                    </section>
                  ))}
                  <section className="chain-trace-response-data">
                    <h3>{t('响应数据')}</h3>
                    <pre>{json(responseData)}</pre>
                  </section>
                </>
              )}
            </div>
          )}
          {activeTab === 'usage' && (
            <section className="chain-trace-usage">
              <h3 className="chain-trace-section-title">{t('Token 用量')}</h3>
              <dl className="chain-trace-summary">
                {usageRows.map(({ key, path, value }) => (
                  <div key={key || 'usage'}>
                    <dt>{(usageLabels[key] ?? path.join(' / ')) || t('用量')}</dt>
                    <dd>{typeof value === 'number'
                      ? new Intl.NumberFormat(locale).format(value)
                      : value == null ? t('不可用') : String(value)}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
          {activeTab === 'timing' && (
            <dl className="chain-trace-summary">
              <div><dt>{t('开始时间')}</dt><dd>{new Date(entry.startedAt).toLocaleString()}</dd></div>
              <div><dt>{t('总时长')}</dt><dd>{total == null ? traceStatusLabel(entry.status, t) : durationLabel(total, t)}</dd></div>
              <div><dt>{t('首 token 延迟')}</dt><dd>{ttft == null ? t('不可用') : durationLabel(ttft, t)}</dd></div>
            </dl>
          )}
          {activeTab === 'result' && (
            <pre>{entry.failure
              ? json({
                  errorType: entry.failure.errorType,
                  message: entry.failure.message ?? null,
                })
              : entry.resultOmitted ? t('结果内容未保留') : json(entry.result)}</pre>
          )}
        </div>
        <OverlayScrollbar viewportRef={bodyRef} />
      </div>
    </>
  )
  const details = overlay ? (
    <dialog
      ref={dialogRef}
      open
      id="chain-trace-details"
      className="chain-trace-details"
      aria-modal="true"
      aria-label={t('链路详情')}
      tabIndex={-1}
      onKeyDown={trapFocus}
    >
      {detailsBody}
    </dialog>
  ) : (
    <aside
      id="chain-trace-details"
      className="chain-trace-details"
      aria-label={t('链路详情')}
    >
      {detailsBody}
    </aside>
  )
  if (!overlay) return details
  return createPortal(
    // 遮罩只响应抽屉外的指针操作，键盘关闭与焦点循环由抽屉自身负责
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      ref={overlayRef}
      className="modal-backdrop chain-trace-details-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      {details}
    </div>,
    document.body,
  )
}

export function ChainTraceView({
  threadId,
  active,
  live,
  observedAt,
  onError,
}: {
  threadId: string
  active: boolean
  live: boolean
  observedAt?: string
  onError?: (message: string) => void
}) {
  const { locale, t } = useI18n()
  const latestErrorHandler = useRef(onError)
  latestErrorHandler.current = onError
  const [isSearchOpen, setSearchOpen] = useState(false)
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string>()
  const detailTrigger = useRef<HTMLButtonElement | null>(null)
  const manualClose = useRef(false)
  const scrollLatestIntoView = useRef(false)
  const initiallyScrolledThread = useRef<string | undefined>(undefined)
  const contentRef = useRef<HTMLDivElement>(null)
  const traceRegion = useRef<HTMLElement>(null)
  const searchControlRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const searchTriggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const timer = window.setTimeout(
      () => setSearchQuery(searchInput.trim()),
      TRACE_SEARCH_DELAY_MS,
    )
    return () => window.clearTimeout(timer)
  }, [searchInput])

  useEffect(() => {
    if (!isSearchOpen) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && searchControlRef.current?.contains(target)) return
      if (searchInput.trim()) searchInputRef.current?.blur()
      else setSearchOpen(false)
    }
    document.addEventListener('pointerdown', handleOutsidePointerDown)
    return () => document.removeEventListener('pointerdown', handleOutsidePointerDown)
  }, [isSearchOpen, searchInput])

  const filter = useMemo(() => ({
    query: searchQuery || undefined,
  }), [searchQuery])
  const trace = useChainTrace({ threadId, active, live, observedAt, filter, limit: 1000 })
  useEffect(() => {
    if (trace.state.phase === 'error') latestErrorHandler.current?.(t('链路加载失败'))
  }, [trace.state.phase, t])
  const page = trace.state.phase === 'ready' ? trace.state.page : undefined
  const graphNodes = useMemo(() => page?.nodes ?? [], [page?.nodes])
  const matchedNodeIds = useMemo(
    () => new Set(page?.matchedNodeIds ?? []),
    [page?.matchedNodeIds],
  )
  const nodes = useMemo(
    () => graphNodes.filter((node) => matchedNodeIds.has(node.id)),
    [graphNodes, matchedNodeIds],
  )
  const nodesById = useMemo(
    () => new Map(graphNodes.map((node) => [node.id, node])),
    [graphNodes],
  )
  const selected = selectedId ? nodesById.get(selectedId) : undefined
  const timelineNodes = useMemo(
    () => selected
      ? nodes.filter((node) => node.turnId === selected.turnId)
      : nodes,
    [nodes, selected],
  )
  const timelineTurns = useMemo(
    () => selected
      ? (page?.turns ?? []).filter((turn) => turn.id === selected.turnId)
      : page?.turns ?? [],
    [page?.turns, selected],
  )
  const timeline = useMemo(
    () => selected
      ? buildTraceTimelineLayout(timelineTurns, timelineNodes)
      : null,
    [selected, timelineNodes, timelineTurns],
  )
  const sequence = useMemo(
    () => buildTraceSequenceLayout(page?.turns ?? [], nodes),
    [nodes, page?.turns],
  )
  const turnRows = useMemo(
    () => groupTraceNodesByTurn(page?.turns ?? [], graphNodes),
    [graphNodes, page?.turns],
  )
  const timelineTurn = selected
    ? timelineTurns.find((turn) => turn.id === selected.turnId)
    : undefined
  const turnSummary = timelineTurn
    ? locale === 'en'
      ? <>{t('第')} <strong>{timelineTurn.ordinal}</strong></>
      : <>{t('第')} <strong>{timelineTurn.ordinal}</strong> {t('轮')}</>
    : locale === 'en'
      ? <>{t('共')} <strong>{timelineTurns.length}</strong> {t('轮')}{timelineTurns.length === 1 ? '' : 's'}</>
      : <>{t('共')} <strong>{timelineTurns.length}</strong> {t('轮')}</>
  const selectedPosition = useMemo(() => {
    if (!selected) return undefined
    for (const { turn, nodes: turnNodes } of turnRows) {
      const index = turnNodes.findIndex((node) => node.id === selected.id)
      if (index >= 0) return { turnOrdinal: turn.ordinal, stepOrdinal: index + 1 }
    }
    return undefined
  }, [selected, turnRows])
  const localResponseEntries = useMemo(() => nodes.filter((node) => (
    node.modelCallId === selected?.id
    && (
      node.kind === 'assistant_message'
      || node.kind === 'tool'
      || node.kind === 'subagent'
    )
  )), [nodes, selected?.id])
  const mainResponseEntriesComplete = !searchQuery
    && !page?.completeness.callTrackingMissing
    && !page?.completeness.relationshipEvidenceMissing
    && !page?.completeness.detailsOmitted
  const responseQueryEnabled = selected?.kind === 'model'
    && !mainResponseEntriesComplete
  const responseRevision = selected?.kind === 'model'
    ? [
        page?.asOfSeq ?? 0,
        selected.updatedSeq,
        ...localResponseEntries.map((entry) => `${entry.id}:${entry.updatedSeq}`),
      ].join('|')
    : ''
  const modelResponse = useTraceModelResponse({
    threadId,
    modelId: selected?.kind === 'model' ? selected.id : undefined,
    enabled: responseQueryEnabled,
    responseRevision,
  })
  const queriedResponseMatches = modelResponse.state.phase !== 'idle'
    && modelResponse.state.modelId === selected?.id
    && modelResponse.state.responseRevision === responseRevision
  const responseStatus = responseQueryEnabled
    ? queriedResponseMatches ? modelResponse.state.phase : 'loading'
    : 'ready'
  useEffect(() => {
    if (responseStatus === 'error') latestErrorHandler.current?.(t('完整响应加载失败'))
  }, [responseStatus, t])
  const responseEntries = responseQueryEnabled
    && queriedResponseMatches
    && modelResponse.state.phase === 'ready'
    ? mergeTraceNodes(localResponseEntries, modelResponse.state.entries)
    : responseQueryEnabled ? [] : localResponseEntries
  const detailsOverlay = useTraceDetailsOverlay(contentRef, nodes.length > 0)
  const incomplete = page?.nextCursor != null
  const completenessMessage = page?.completeness.relationshipEvidenceMissing
    ? t('部分节点缺少完整关联依据')
    : page?.completeness.callTrackingMissing
      ? t('部分历史运行没有调用级跟踪数据')
      : page?.completeness.detailsOmitted
        ? t('部分链路内容未保留')
        : undefined

  useEffect(() => {
    manualClose.current = false
    detailTrigger.current = null
    setSelectedId(undefined)
  }, [threadId])

  useLayoutEffect(() => {
    if (
      !active || !page || incomplete || nodes.length === 0
      || initiallyScrolledThread.current === threadId
    ) return
    const ledger = traceRegion.current?.querySelector<HTMLElement>('.chain-trace-ledger')
    if (!ledger) return
    ledger.scrollTop = ledger.scrollHeight
    initiallyScrolledThread.current = threadId
  }, [active, incomplete, nodes.length, page, threadId])

  useEffect(() => {
    if (!page || incomplete || nodes.length === 0 || detailsOverlay !== false) return
    if (selectedId && nodesById.has(selectedId)) return
    if (manualClose.current && !selectedId) return
    detailTrigger.current = null
    const preferred = preferredTraceNode(nodes)
    if (!preferred) return
    scrollLatestIntoView.current = true
    setSelectedId(preferred.id)
  }, [detailsOverlay, incomplete, nodes, nodesById, page, selectedId])

  useLayoutEffect(() => {
    if (!scrollLatestIntoView.current || !selectedId) return
    scrollLatestIntoView.current = false
    const ledger = traceRegion.current?.querySelector<HTMLElement>('.chain-trace-ledger')
    if (ledger) ledger.scrollTop = ledger.scrollHeight
  }, [selectedId])

  useEffect(() => {
    if (!selectedId) return undefined
    const closeOnEscape = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented
        || event.key !== 'Escape'
        || (event.target instanceof Element && event.target.closest('[role="listbox"]'))
      ) return
      event.preventDefault()
      const trigger = detailTrigger.current
      const nodeId = selectedId
      manualClose.current = true
      setSelectedId(undefined)
      restoreTraceNodeFocus(traceRegion.current, nodeId, trigger)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [selectedId])

  const locateNode = (nodeId: string) => {
    window.requestAnimationFrame(() => {
      const target = findTraceNodeTrigger(traceRegion.current, nodeId)
      target?.scrollIntoView?.({
        block: 'center',
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'auto'
          : 'smooth',
      })
    })
  }

  const selectEntry = (
    entryId: string,
    trigger: HTMLButtonElement,
    locate = false,
  ) => {
    manualClose.current = false
    detailTrigger.current = trigger
    setSelectedId(entryId)
    if (locate) locateNode(entryId)
  }

  const closeDetails = () => {
    const trigger = detailTrigger.current
    const nodeId = selectedId
    manualClose.current = true
    setSelectedId(undefined)
    restoreTraceNodeFocus(traceRegion.current, nodeId, trigger)
  }

  const hideDetailsForCollapse = (fallback: HTMLButtonElement) => {
    manualClose.current = true
    detailTrigger.current = fallback
    setSelectedId(undefined)
    window.requestAnimationFrame(() => fallback.focus())
  }

  const openSearch = () => {
    setSearchOpen(true)
    window.requestAnimationFrame(() => searchInputRef.current?.focus())
  }

  const clearAndCloseSearch = () => {
    setSearchInput('')
    setSearchQuery('')
    setSearchOpen(false)
    window.requestAnimationFrame(() => searchTriggerRef.current?.focus())
  }

  if (!threadId) {
    return (
      <section ref={traceRegion} className="chain-trace" role="tabpanel" aria-label={t('链路')}>
        <div className="chain-trace-empty"><Activity size={28} /><h2>{t('链路')}</h2><p>{t('发起会话后可查看真实调用链')}</p></div>
      </section>
    )
  }

  return (
    <section ref={traceRegion} id="chain-trace-panel" className="chain-trace" role="tabpanel" aria-label={t('链路')}>
      {trace.state.phase === 'ready' && (
        <div
          className="chain-trace-toolbar-host"
          aria-hidden={detailsOverlay === true && Boolean(selected) || undefined}
          inert={detailsOverlay === true && Boolean(selected) || undefined}
        >
          <div className="chain-trace-toolbar" aria-label={t('链路操作')}>
          <div className="chain-trace-toolbar-context">
            <div className="chain-trace-range-summary" aria-live="polite">
              <span>{turnSummary}</span>
              <i aria-hidden="true">/</i>
              <span><strong>{timelineNodes.length}</strong> {t('节点')}</span>
            </div>
            {completenessMessage && (
              <span className="chain-trace-completeness" role="status">
                {completenessMessage}
              </span>
            )}
          </div>
          <div className="chain-trace-actions">
            <div
              ref={searchControlRef}
              className={`chain-trace-search-control${isSearchOpen ? ' is-open' : ''}`}
            >
              {!isSearchOpen ? (
                <IconButton
                  ref={searchTriggerRef}
                  className="chain-trace-search-trigger"
                  size="sm"
                  label={t('搜索链路节点')}
                  tooltip={t('搜索链路节点')}
                  icon={<Search size={18} />}
                  selected={Boolean(searchInput)}
                  aria-expanded="false"
                  aria-controls="chain-trace-search"
                  onClick={openSearch}
                />
              ) : (
                <SearchField
                  id="chain-trace-search"
                  className="chain-trace-search"
                  ref={searchInputRef}
                  label={t('搜索链路节点')}
                  closeLabel={t('清除链路搜索')}
                  placeholder={t('搜索节点、内容')}
                  value={searchInput}
                  onChange={setSearchInput}
                  onClose={clearAndCloseSearch}
                />
              )}
            </div>
          </div>
          </div>
        </div>
      )}

      {trace.state.phase === 'loading' && (
        <div className="chain-trace-state is-feedback">
          <FeedbackState kind="loading" title={t('正在加载链路…')} />
        </div>
      )}
      {trace.state.phase === 'error' && (
        <div className="chain-trace-state is-feedback">
          <Button type="button" variant="text" onClick={trace.retry}>{t('重新加载')}</Button>
        </div>
      )}
      {incomplete && (
        <div className="chain-trace-state is-warning" role="status">
          <p>{t('链路超过完整视图上限，请使用搜索缩小范围')}</p>
        </div>
      )}
      {trace.state.phase === 'ready' && !incomplete && nodes.length === 0 && (
        <div className="chain-trace-state"><Activity size={24} /><p>{t('没有匹配的链路节点')}</p></div>
      )}
      {trace.state.phase === 'ready' && !incomplete && nodes.length > 0 && (
        <>
          {selected && timeline ? (
            <TraceTimeline
              mode="timeline"
              layout={timeline}
              selected={selected}
              backgroundInert={detailsOverlay === true}
              onSelect={(nodeId, trigger) => selectEntry(nodeId, trigger, true)}
            />
          ) : (
            <TraceTimeline
              mode="sequence"
              layout={sequence}
              backgroundInert={false}
              onSelect={(nodeId, trigger) => selectEntry(nodeId, trigger, true)}
            />
          )}
          <div
            ref={contentRef}
            className={`chain-trace-content-grid${selected ? ' has-details' : ''}${detailsOverlay ? ' uses-overlay' : ''}`}
          >
            <TraceLedger
              groups={turnRows}
              directNodeIds={matchedNodeIds}
              selectedId={selectedId}
              backgroundInert={detailsOverlay === true && Boolean(selected)}
              onSelect={selectEntry}
              onHideSelection={hideDetailsForCollapse}
            />
            {selected && (
              <TraceDetails
                key={selected.id}
                entry={selected}
                responseEntries={responseEntries}
                responseStatus={responseStatus === 'idle' ? 'loading' : responseStatus}
                turnOrdinal={selectedPosition?.turnOrdinal}
                stepOrdinal={selectedPosition?.stepOrdinal}
                overlay={detailsOverlay === true}
                onRetryResponse={modelResponse.retry}
                onClose={closeDetails}
              />
            )}
          </div>
        </>
      )}
    </section>
  )
}
