import type {
  ReadyTaskTraceSnapshot,
  TaskTraceSnapshot,
  TodoGroup,
} from '../../../api/conversation/taskTrace'
import type { ConversationAgUiEvent } from '../../../api/conversation/types'
import type { DeepReadonly, JsonObject, Message } from '../../../types'
import {
  normalizeUserMessagePreview,
  parseRootTodos,
  projectTodoItems,
  resolveTodoGroupStatus,
  todoGroupId,
  type RootTodoValue,
} from './domain'

type RunInputKind = 'ordinary' | 'resume' | 'abandon'
type ToolResult = 'pending' | 'succeeded' | 'failed'

export interface LiveTurnSeed {
  runId: string
  userMessageId: string
  userMessagePreview: string
}

interface RunState {
  runId: string
  inputKind: RunInputKind
  parentRunId?: string
  turnId: string
  terminal: boolean
  initializationFailed: boolean
}

interface ToolCallState {
  id: string
  turnId: string
  runId: string
  startedAt: string
  result: ToolResult
}

interface TurnState {
  id: string
  originRunId: string
  userMessageId: string
  userMessagePreview: string
  calls: ToolCallState[]
  selectedCallId?: string
  groupId?: string
}

const rawContext = (event: ConversationAgUiEvent) => {
  if (event.type === 'RAW') return undefined
  return 'rawEvent' in event ? event.rawEvent : undefined
}

const rootEvent = (event: ConversationAgUiEvent) => {
  const raw = rawContext(event)
  return raw?.source == null || raw.source.kind === 'root'
}

const eventRunId = (
  event: ConversationAgUiEvent,
  fallback: string | undefined,
) => rawContext(event)?.runId ?? fallback

const stateTouchesTodos = (event: ConversationAgUiEvent) => {
  if (event.type === 'STATE_SNAPSHOT') return Object.hasOwn(event.snapshot, 'todos')
  return event.type === 'STATE_DELTA' && event.delta.some(
    (operation) => operation.path === '/todos' || operation.path.startsWith('/todos/'),
  )
}

/** 维护当前浏览器会话中可丢弃的实时任务轨迹 */
export class LiveTodoTraceProjector {
  private runs = new Map<string, RunState>()
  private heads = new Set<string>()
  private turns = new Map<string, TurnState>()
  private calls = new Map<string, ToolCallState>()
  private ignoredCalls = new Set<string>()
  private groups: TodoGroup[] = []
  private latestRootTodos: RootTodoValue[] = []
  private latestRootTodosAt?: string
  private currentRunId?: string
  private failed = false
  private currentSnapshot: TaskTraceSnapshot = { status: 'ready', todoGroups: [] }

  get snapshot(): TaskTraceSnapshot {
    return this.currentSnapshot
  }

  hydrate(
    snapshot: ReadyTaskTraceSnapshot,
    options: { headRunId: string; messages: readonly Message[]; latestTurn?: LiveTurnSeed; isRunning?: boolean },
  ) {
    if (this.runs.size > 0 || this.groups.length > 0) {
      throw new Error('实时任务轨迹只能从权威快照初始化一次')
    }
    // 权威 snapshot 对外为倒序；内部保持创建顺序，便于同毫秒时用 append 次序判定新旧
    this.groups = [...snapshot.todoGroups].reverse()
    this.currentSnapshot = snapshot
    const latestGroup = snapshot.todoGroups[0]
    const latestGroupOriginRunId = latestGroup?.id.startsWith('todo-group:')
      ? latestGroup.id.slice('todo-group:'.length)
      : undefined
    const turnSeed = options.latestTurn ?? (latestGroup
      ? {
          runId: latestGroupOriginRunId ?? options.headRunId,
          userMessageId: latestGroup.userMessageId,
          userMessagePreview: latestGroup.userMessagePreview,
        }
      : undefined)
    const turnId = `turn:${turnSeed?.runId ?? options.headRunId}`
    if (turnSeed) {
      this.turns.set(turnId, {
        id: turnId,
        originRunId: turnSeed.runId,
        userMessageId: turnSeed.userMessageId,
        userMessagePreview: normalizeUserMessagePreview(turnSeed.userMessagePreview),
        calls: [],
        ...(latestGroup && latestGroupOriginRunId === turnSeed.runId
          ? { groupId: latestGroup.id }
          : {}),
      })
    }
    this.runs.set(options.headRunId, {
      runId: options.headRunId,
      inputKind: 'ordinary',
      turnId,
      terminal: !options.isRunning,
      initializationFailed: false,
    })
    this.heads.add(options.headRunId)
    if (latestGroup) {
      this.latestRootTodos = latestGroup.todos.map((todo) => ({
        id: todo.id,
        content: todo.content,
        status: todo.status === 'completed'
          ? 'completed'
          : todo.status === 'pending'
            ? 'pending'
            : 'in_progress',
      }))
      this.latestRootTodosAt = latestGroup.createdAt
    }
    // 审批后的结果可属于上次运行；恢复同一提问中的工具，保留 Todo 候选顺序
    const turn = this.turns.get(turnId)
    let questionIndex = options.messages.length - 1
    while (questionIndex >= 0 && options.messages[questionIndex]?.role !== 'user') questionIndex -= 1
    for (const message of options.messages.slice(questionIndex + 1)) {
      const meta = message.meta
      if (message.role !== 'tool' || meta?.graphNamespace?.length !== 0 || !meta.toolCallId) continue
      if (meta.toolName !== 'write_todos') {
        this.ignoredCalls.add(meta.toolCallId)
        continue
      }
      if (!turn) continue
      const call: ToolCallState = {
        id: meta.toolCallId,
        turnId,
        runId: meta.runId ?? options.headRunId,
        startedAt: message.createdAt,
        result: meta.status === 'completed' ? 'succeeded'
          : meta.status === 'failed' || meta.status === 'cancelled' ? 'failed' : 'pending',
      }
      this.calls.set(call.id, call)
      turn.calls.push(call)
    }
    this.selectCandidate(turn)
  }

  startRun({
    runId,
    inputKind,
    parentRunId,
    turn,
  }: {
    runId: string
    inputKind: RunInputKind
    parentRunId?: string
    turn?: LiveTurnSeed
  }) {
    if (this.failed) return
    const existing = this.runs.get(runId)
    if (existing) {
      if (existing.inputKind !== inputKind || existing.parentRunId !== parentRunId) {
        this.fail('trace_incomplete')
      }
      this.currentRunId = runId
      return
    }
    let resolvedParent = parentRunId
    if (resolvedParent) {
      const parent = this.runs.get(resolvedParent)
      if (!parent?.terminal) {
        this.fail('trace_incomplete')
        return
      }
    } else if (this.runs.size > 0) {
      const candidates = [...this.heads].filter((id) => this.runs.get(id)?.terminal)
      if (candidates.length !== 1) {
        this.fail('trace_incomplete')
        return
      }
      resolvedParent = candidates[0]
    }

    let turnId: string
    if (inputKind === 'ordinary') {
      if (!turn || turn.runId !== runId) {
        this.fail('trace_incomplete')
        return
      }
      turnId = `turn:${runId}`
      const preview = normalizeUserMessagePreview(turn.userMessagePreview)
      if (!preview || !turn.userMessageId.trim()) {
        this.fail('trace_incomplete')
        return
      }
      this.turns.set(turnId, {
        id: turnId,
        originRunId: runId,
        userMessageId: turn.userMessageId,
        userMessagePreview: preview,
        calls: [],
      })
    } else {
      const parentTurnId = resolvedParent
        ? this.runs.get(resolvedParent)?.turnId
        : undefined
      if (!parentTurnId) {
        this.fail('trace_incomplete')
        return
      }
      turnId = parentTurnId
    }
    this.runs.set(runId, {
      runId,
      inputKind,
      ...(resolvedParent ? { parentRunId: resolvedParent } : {}),
      turnId,
      terminal: false,
      initializationFailed: false,
    })
    if (resolvedParent) this.heads.delete(resolvedParent)
    this.heads.add(runId)
    this.currentRunId = runId
  }

  consume(
    event: ConversationAgUiEvent,
    options: { receivedAt: string; rootState?: DeepReadonly<JsonObject> },
  ): TaskTraceSnapshot {
    if (this.failed) return this.currentSnapshot
    if (event.type === 'RUN_STARTED') {
      if (!this.runs.has(event.runId)) this.fail('trace_incomplete')
      else this.currentRunId = event.runId
      return this.currentSnapshot
    }
    if (event.type === 'TOOL_CALL_START' && rootEvent(event)) {
      const runId = eventRunId(event, this.currentRunId)
      const run = runId ? this.runs.get(runId) : undefined
      if (!run) return this.fail('trace_incomplete')
      if (event.toolCallName !== 'write_todos') {
        this.ignoredCalls.add(event.toolCallId)
        return this.currentSnapshot
      }
      if (this.calls.has(event.toolCallId)) return this.fail('trace_incomplete')
      const call: ToolCallState = {
        id: event.toolCallId,
        turnId: run.turnId,
        runId: run.runId,
        startedAt: options.receivedAt,
        result: 'pending',
      }
      this.calls.set(call.id, call)
      this.turns.get(run.turnId)?.calls.push(call)
      return this.currentSnapshot
    }
    if (event.type === 'TOOL_CALL_RESULT' && rootEvent(event)) {
      if (this.ignoredCalls.has(event.toolCallId)) return this.currentSnapshot
      const call = this.calls.get(event.toolCallId)
      const runId = eventRunId(event, this.currentRunId)
      if (!call || !runId || call.turnId !== this.runs.get(runId)?.turnId) {
        return this.fail('trace_incomplete')
      }
      const result = event.rawEvent?.toolResultStatus === 'error'
        ? 'failed'
        : 'succeeded'
      if (call.result !== 'pending' && call.result !== result) {
        return this.fail('trace_incomplete')
      }
      call.result = result
      this.selectCandidate(this.turns.get(call.turnId))
      return this.currentSnapshot
    }
    if (
      (event.type === 'STATE_SNAPSHOT' || event.type === 'STATE_DELTA')
      && rootEvent(event)
      && stateTouchesTodos(event)
    ) {
      const rawTodos = options.rootState?.todos
      if (rawTodos === undefined) return this.fail('todo_state_invalid')
      const todos = parseRootTodos(rawTodos)
      if (!todos) return this.fail('todo_state_invalid')
      this.latestRootTodos = todos
      this.latestRootTodosAt = options.receivedAt
      const runId = eventRunId(event, this.currentRunId)
      const turn = runId ? this.turns.get(this.runs.get(runId)?.turnId ?? '') : undefined
      if (!turn) return this.fail('trace_incomplete')
      if (turn.groupId) this.updateGroupTodos(turn.groupId, todos)
      else if (turn.selectedCallId && todos.length > 0) this.createGroup(turn, todos)
      return this.currentSnapshot
    }
    if (
      (event.type === 'TEXT_MESSAGE_START'
        || event.type === 'TEXT_MESSAGE_CONTENT'
        || event.type === 'TEXT_MESSAGE_END')
      && rootEvent(event)
    ) {
      const runId = eventRunId(event, this.currentRunId)
      const turn = runId ? this.turns.get(this.runs.get(runId)?.turnId ?? '') : undefined
      if (turn) this.syncCandidate(turn)
      return this.currentSnapshot
    }
    if (event.type === 'RUN_FINISHED') {
      const run = this.runs.get(event.runId)
      if (!run) return this.fail('trace_incomplete')
      run.terminal = true
      const turn = this.turns.get(run.turnId)
      if (event.outcome?.type === 'interrupt') {
        if (turn) this.syncCandidate(turn)
      } else if (turn) {
        this.syncCandidate(turn)
        this.updateGroupStatus(turn.groupId, 'completed')
      }
      return this.currentSnapshot
    }
    if (event.type === 'RUN_ERROR' && rootEvent(event)) {
      const runId = eventRunId(event, this.currentRunId)
      const run = runId ? this.runs.get(runId) : undefined
      if (!run) return this.fail('trace_incomplete')
      run.terminal = true
      run.initializationFailed = event.rawEvent?.initializationFailed === true
      if (run.initializationFailed) return this.currentSnapshot
      const turn = this.turns.get(run.turnId)
      const cancelled = event.code === 'cancelled' || event.code === 'resume_cancelled'
      this.updateGroupStatus(turn?.groupId, cancelled ? 'cancelled' : 'failed')
    }
    return this.currentSnapshot
  }

  close() {
    this.runs.clear()
    this.heads.clear()
    this.turns.clear()
    this.calls.clear()
    this.ignoredCalls.clear()
    this.groups = []
    this.latestRootTodos = []
    this.latestRootTodosAt = undefined
    this.currentSnapshot = { status: 'ready', todoGroups: [] }
  }

  private selectCandidate(turn: TurnState | undefined) {
    if (!turn || turn.groupId || turn.selectedCallId) return
    for (const call of turn.calls) {
      if (call.result === 'failed') continue
      if (call.result === 'pending') return
      turn.selectedCallId = call.id
      if (
        this.latestRootTodos.length > 0
        && this.latestRootTodosAt != null
        && Date.parse(this.latestRootTodosAt) > Date.parse(call.startedAt)
      ) this.createGroup(turn, this.latestRootTodos)
      return
    }
  }

  private syncCandidate(turn: TurnState) {
    // 相同 todos 不会产生新 state event，安全边界沿用最近权威根状态
    if (!turn.groupId && turn.selectedCallId && this.latestRootTodos.length > 0) {
      this.createGroup(turn, this.latestRootTodos)
    }
  }

  private createGroup(turn: TurnState, todos: RootTodoValue[]) {
    const call = turn.selectedCallId ? this.calls.get(turn.selectedCallId) : undefined
    if (!call) return this.fail('trace_incomplete')
    const id = todoGroupId(turn.originRunId)
    const group: TodoGroup = {
      id,
      userMessageId: turn.userMessageId,
      userMessagePreview: turn.userMessagePreview,
      groupToolCallId: call.id,
      createdAt: call.startedAt,
      status: 'running',
      todos: projectTodoItems(todos, id, 'running'),
    }
    turn.groupId = id
    this.groups = [...this.groups, group]
    this.publish()
    return this.currentSnapshot
  }

  private updateGroupTodos(groupId: string, todos: RootTodoValue[]) {
    this.groups = this.groups.map((group) => {
      if (group.id !== groupId) return group
      const status = resolveTodoGroupStatus(todos, group.status)
      return { ...group, status, todos: projectTodoItems(todos, group.id, status) }
    })
    this.publish()
  }

  private updateGroupStatus(
    groupId: string | undefined,
    status: TodoGroup['status'],
  ) {
    if (!groupId) return
    status = resolveTodoGroupStatus(this.latestRootTodos, status)
    this.groups = this.groups.map((group) => group.id === groupId
      ? { ...group, status, todos: projectTodoItems(this.latestRootTodos, group.id, status) }
      : group)
    this.publish()
  }

  private publish() {
    const todoGroups = this.groups
      .map((group, createdOrder) => ({ group, createdOrder }))
      .sort((left, right) => (
        Date.parse(right.group.createdAt) - Date.parse(left.group.createdAt)
        || right.createdOrder - left.createdOrder
      ))
      .map(({ group }) => group)
    this.currentSnapshot = { status: 'ready', todoGroups } satisfies ReadyTaskTraceSnapshot
  }

  private fail(errorCode: 'trace_incomplete' | 'todo_state_invalid') {
    this.failed = true
    this.groups = []
    this.latestRootTodos = []
    this.latestRootTodosAt = undefined
    this.currentSnapshot = { status: 'unavailable', todoGroups: [], errorCode }
    return this.currentSnapshot
  }
}
