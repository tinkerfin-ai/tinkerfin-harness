import { describe, expect, it } from 'vitest'

import { traceGraphNode } from '../../../test/traceFixtures'
import {
  buildTraceSequenceLayout,
  buildTraceTimelineLayout,
  groupTraceNodesByTurn,
  preferredTraceNode,
  traceRailAncestors,
} from './traceLayout'

const startedAt = '2026-09-03T00:00:00.000Z'
const turn = { id: 'turn-fixture', ordinal: 1, startedAt }

describe('Chain Trace layout derivation', () => {
  it('keeps the root line through a tool child and ends a final child branch', () => {
    const tool = traceGraphNode({ id: 'tool', kind: 'tool' })
    const context = traceGraphNode({ id: 'context', kind: 'context', parentNodeId: 'tool' })
    const next = traceGraphNode({ id: 'next', kind: 'model' })
    expect(traceRailAncestors([tool, context, next]).get('context')).toEqual({ ancestorLevels: [0], continues: false })
    expect(traceRailAncestors([tool, context, next]).get('tool')).toEqual({ ancestorLevels: [], continues: true })
    expect(traceRailAncestors([tool, context]).get('context')).toEqual({ ancestorLevels: [], continues: false })
  })

  it('continues only ancestors with later branches in the visible tree', () => {
    const outer = traceGraphNode({ id: 'outer', kind: 'subagent' })
    const inner = traceGraphNode({ id: 'inner', kind: 'subagent', parentSubagentId: 'outer' })
    const leaf = traceGraphNode({ id: 'leaf', kind: 'model', parentSubagentId: 'inner' })
    const sibling = traceGraphNode({ id: 'sibling', kind: 'tool', parentSubagentId: 'outer' })
    expect(traceRailAncestors([outer, inner, leaf, sibling]).get('leaf')).toEqual({ ancestorLevels: [1], continues: false })
    expect(traceRailAncestors([outer, inner, leaf]).get('leaf')).toEqual({ ancestorLevels: [], continues: false })
    expect(traceRailAncestors([outer, inner, sibling]).get('sibling')).toEqual({ ancestorLevels: [], continues: false })
  })

  it('packs overlapping intervals into deterministic tracks and retains zero-duration nodes', () => {
    const layout = buildTraceTimelineLayout([turn], [
      traceGraphNode({ id: 'model-a', kind: 'model', startedAt, completedAt: '2026-09-03T00:00:02.000Z' }),
      traceGraphNode({ id: 'model-b', kind: 'model', startedSeq: 2, startedAt: '2026-09-03T00:00:01.000Z', completedAt: '2026-09-03T00:00:03.000Z' }),
      traceGraphNode({ id: 'message', kind: 'assistant_message', startedSeq: 3, startedAt: '2026-09-03T00:00:03.000Z', completedAt: '2026-09-03T00:00:03.000Z' }),
    ])

    const model = layout?.lanes.find((lane) => lane.id === 'model')
    const assistant = layout?.lanes.find((lane) => lane.id === 'assistant')
    expect(model?.trackCount).toBe(2)
    expect(model?.bars.map((bar) => bar.track)).toEqual([0, 1])
    expect(assistant?.trackCount).toBe(1)
    expect(assistant?.bars.find((bar) => bar.node.id === 'message')?.widthPercent).toBe(0)
  })

  it('keeps running durations stable and preserves authoritative Turn order', () => {
    const running = traceGraphNode({
      id: 'running',
      status: 'running',
      startedAt,
      completedAt: null,
    })
    expect(buildTraceTimelineLayout([turn], [running])?.durationMilliseconds).toBe(1)
    expect(groupTraceNodesByTurn([
      { id: 'turn', ordinal: 1, startedAt },
    ], [
      traceGraphNode({ id: 'first', turnId: 'turn', startedSeq: 1 }),
      traceGraphNode({ id: 'second', turnId: 'turn', startedSeq: 2 }),
    ])[0]?.nodes.map((node) => node.id)).toEqual(['first', 'second'])
  })

  it('concatenates Turn execution while excluding the user wait between Turns', () => {
    const secondTurnStart = '2026-09-03T01:00:00.000Z'
    const layout = buildTraceTimelineLayout([
      turn,
      { id: 'turn-2', ordinal: 2, startedAt: secondTurnStart },
    ], [
      traceGraphNode({
        id: 'turn-1-user',
        turnId: turn.id,
        kind: 'human_message',
        startedAt,
        completedAt: '2026-09-03T00:00:02.000Z',
      }),
      traceGraphNode({
        agui: null,
        id: 'turn-2-user',
        turnId: 'turn-2',
        kind: 'human_message',
        startedSeq: 2,
        startedAt: secondTurnStart,
        completedAt: '2026-09-03T01:00:01.000Z',
      }),
    ])

    expect(layout?.durationMilliseconds).toBe(3_000)
    const bars = layout?.lanes.find((lane) => lane.id === 'user')?.bars ?? []
    expect(bars.map(({ startMilliseconds, endMilliseconds }) => (
      [startMilliseconds, endMilliseconds]
    ))).toEqual([[0, 2_000], [2_000, 3_000]])
    expect(bars[1]?.leftPercent).toBeCloseTo(66.67, 1)
  })

  it('maps every overview node to one ordered sequence column', () => {
    const layout = buildTraceSequenceLayout([
      turn,
      { id: 'turn-2', ordinal: 2, startedAt },
    ], [
      traceGraphNode({ id: 'user', kind: 'human_message' }),
      traceGraphNode({ id: 'model', kind: 'model', startedSeq: 2 }),
      traceGraphNode({
      agui: null, id: 'tool', turnId: 'turn-2', kind: 'tool', startedSeq: 3 }),
      traceGraphNode({
        agui: null,
        id: 'assistant',
        turnId: 'turn-2',
        kind: 'assistant_message',
        startedSeq: 4,
      }),
    ])

    expect(layout.columnCount).toBe(4)
    expect(layout.lanes.map((lane) => lane.id)).toEqual([
      'user',
      'model',
      'tool',
      'assistant',
    ])
    expect(layout.lanes.flatMap((lane) => lane.items).map((item) => (
      [item.node.id, item.column]
    ))).toEqual([
      ['user', 1],
      ['model', 2],
      ['tool', 3],
      ['assistant', 4],
    ])
    expect(layout.turnBoundaries).toEqual([2])
  })

  it('prefers the latest failure and otherwise the latest node', () => {
    const nodes = [
      traceGraphNode({ id: 'success-latest', startedSeq: 4 }),
      traceGraphNode({ id: 'failure-old', startedSeq: 2, status: 'failed' }),
      traceGraphNode({ id: 'failure-new', startedSeq: 3, failure: { errorType: 'Error' } }),
    ]
    expect(preferredTraceNode(nodes)?.id).toBe('failure-new')
    expect(preferredTraceNode(nodes.filter((node) => !node.failure && node.status !== 'failed'))?.id)
      .toBe('success-latest')
  })
})
