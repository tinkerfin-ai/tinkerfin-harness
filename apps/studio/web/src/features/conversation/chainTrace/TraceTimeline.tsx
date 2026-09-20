import {
  Cpu,
  FolderOpen,
  GitFork,
  Link2,
  MessageCircleMore,
  WandSparkles,
} from 'lucide-react'
import { useRef } from 'react'
import type { CSSProperties } from 'react'

import type { TraceGraphNode } from '../../../api/conversation/traceGraph'
import { OverlayScrollbar } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import {
  durationLabel,
  traceNodeAccessibleLabel,
  traceStatusLabel,
} from './tracePresentation'
import type {
  TraceSequenceLayout,
  TraceTimelineLane,
  TraceTimelineLaneId,
  TraceTimelineLayout,
} from './traceLayout'

interface TimelineBarStyle extends CSSProperties {
  '--chain-trace-bar-left': string
  '--chain-trace-bar-width': string
  '--chain-trace-bar-track': number
}

interface SequenceItemStyle extends CSSProperties {
  gridColumn: number
  gridRow: number | string
}

type TraceTimelineProps = {
  onSelect: (nodeId: string, trigger: HTMLButtonElement) => void
} & (
  | {
    mode: 'sequence'
    layout: TraceSequenceLayout
  }
  | {
    mode: 'timeline'
    layout: TraceTimelineLayout
    selected?: TraceGraphNode
  }
)

const laneIcon = (lane: TraceTimelineLaneId) => ({
  user: <MessageCircleMore size={14} aria-hidden="true" />,
  context: <FolderOpen size={14} aria-hidden="true" />,
  model: <Cpu size={14} aria-hidden="true" />,
  tool: <Link2 size={14} aria-hidden="true" />,
  subagent: <GitFork size={14} aria-hidden="true" />,
  assistant: <WandSparkles size={14} aria-hidden="true" />,
}[lane])

const laneLabel = (lane: TraceTimelineLaneId, t: ReturnType<typeof useI18n>['t']) => ({
  user: t('用户'),
  context: t('上下文'),
  model: t('模型'),
  tool: t('工具'),
  subagent: t('子智能体'),
  assistant: t('助手'),
}[lane])

const laneHeight = (lane: TraceTimelineLane) => 17 + Math.max(0, lane.trackCount - 1) * 10

export function TraceTimeline(props: TraceTimelineProps) {
  const { t } = useI18n()
  const scrollRef = useRef<HTMLDivElement>(null)
  const selectedBar = props.mode === 'timeline' && props.selected
    ? props.layout.lanes
      .flatMap((lane) => lane.bars)
      .find((bar) => bar.node.id === props.selected?.id)
    : undefined
  const rowTemplate = props.mode === 'timeline'
    ? `16px ${props.layout.lanes.map(laneHeight).map((height) => `${height}px`).join(' ')}`
    : props.layout.lanes.map(() => '17px').join(' ')

  return (
    <section
      className={`chain-trace-timeline${props.mode === 'sequence' ? ' is-sequence' : ''}`}
      aria-label={props.mode === 'sequence' ? t('执行序列') : t('调用时间线')}
    >
      <div className="chain-trace-timeline-labels" style={{ gridTemplateRows: rowTemplate }}>
        {props.mode === 'timeline' && <span aria-hidden="true" />}
        {props.layout.lanes.map((lane) => (
          <span key={lane.id} className={`chain-trace-lane-label is-${lane.id}`}>
            {laneIcon(lane.id)}
            {laneLabel(lane.id, t)}
          </span>
        ))}
      </div>
      <div className="chain-trace-timeline-scroll-host">
        <div
          ref={scrollRef}
          className="chain-trace-timeline-scroll"
          role="region"
          aria-label={props.mode === 'sequence' ? t('执行序列图表') : t('时间线图表')}
          tabIndex={0}
        >
          {props.mode === 'sequence' ? (
            <div className="chain-trace-timeline-chart is-sequence">
              <div
                className="chain-trace-sequence-grid"
                style={{
                  gridTemplateColumns: `repeat(${props.layout.columnCount}, minmax(calc(var(--space-6) + var(--space-1)), 1fr))`,
                  gridTemplateRows: rowTemplate,
                }}
              >
                {props.layout.turnBoundaries.map((boundary) => (
                  <i
                    key={boundary}
                    className="chain-trace-sequence-turn-boundary"
                    style={{ gridColumn: boundary + 1, gridRow: '1 / -1' }}
                    aria-hidden="true"
                  />
                ))}
                {props.layout.lanes.flatMap((lane, laneIndex) => (
                  lane.items.map((item) => (
                    <button
                      key={item.node.id}
                      type="button"
                      className={`chain-trace-sequence-block is-${lane.id}${item.node.failure ? ' has-error' : ''}`}
                      style={{
                        gridColumn: item.column,
                        gridRow: laneIndex + 1,
                      } as SequenceItemStyle}
                      data-trace-sequence-node-id={item.node.id}
                      aria-label={t('选择 {name}，{status}', {
                        name: traceNodeAccessibleLabel(item.node, t),
                        status: traceStatusLabel(item.node.status, t),
                      })}
                      aria-controls="chain-trace-details"
                      onClick={(event) => props.onSelect(item.node.id, event.currentTarget)}
                    />
                  ))
                ))}
              </div>
            </div>
          ) : (
            <div className="chain-trace-timeline-chart" style={{ gridTemplateRows: rowTemplate }}>
              <div className="chain-trace-ticks">
                {props.layout.ticks.map((tick, index) => (
                  <span
                    key={tick}
                    style={{ left: `${index / (props.layout.ticks.length - 1) * 100}%` }}
                  >
                    {durationLabel(tick, t)}
                  </span>
                ))}
              </div>
              {selectedBar && (
                <div className="chain-trace-timeline-selection-track" aria-hidden="true">
                  <div
                    className="chain-trace-timeline-selection"
                    style={{
                      left: `${selectedBar.leftPercent}%`,
                      width: selectedBar.endMilliseconds === selectedBar.startMilliseconds
                        ? '1px'
                        : `${selectedBar.widthPercent}%`,
                    }}
                  />
                </div>
              )}
              {props.layout.lanes.map((lane) => (
                <div key={lane.id} className={`chain-trace-lane is-${lane.id}`}>
                  {lane.bars.map((bar) => (
                  <button
                    key={bar.node.id}
                    type="button"
                    className={`chain-trace-timeline-bar${bar.node.id === props.selected?.id ? ' is-selected' : ''}${bar.node.failure ? ' has-error' : ''}`}
                    style={{
                      '--chain-trace-bar-left': `${bar.leftPercent}%`,
                      '--chain-trace-bar-width': `${bar.widthPercent}%`,
                      '--chain-trace-bar-track': bar.track,
                    } as TimelineBarStyle}
                    aria-label={bar.node.completedAt
                      ? t('选择 {name}，{status}，{duration}', {
                          name: traceNodeAccessibleLabel(bar.node, t),
                          status: traceStatusLabel(bar.node.status, t),
                          duration: durationLabel(
                            Math.max(0, bar.endMilliseconds - bar.startMilliseconds),
                            t,
                          ),
                        })
                      : t('选择 {name}，{status}', {
                          name: traceNodeAccessibleLabel(bar.node, t),
                          status: traceStatusLabel(bar.node.status, t),
                        })}
                    aria-pressed={bar.node.id === props.selected?.id}
                    aria-controls="chain-trace-details"
                    onClick={(event) => props.onSelect(bar.node.id, event.currentTarget)}
                  />
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
        <OverlayScrollbar viewportRef={scrollRef} axis="horizontal" size="compact" />
      </div>
    </section>
  )
}
