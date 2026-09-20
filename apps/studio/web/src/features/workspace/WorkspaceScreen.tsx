import type { CSSProperties } from 'react'
import { useDrawerLayout } from '../../components/ui/useDrawerLayout'
import { DrawerResizeHandle } from '../../components/ui/DrawerResizeHandle'
import { isConversationRunning } from '../../lib/workspace'
import { AccessModePicker } from "../../components/AccessModePicker"
import { AutomationPage } from '../automation/AutomationPage'
import { AttachmentReferenceContext } from '../conversation/attachments/context'
import { TextRevealProgressContext } from '../conversation/components/textRevealProgress'
import { messageText, messageAttachments } from '../conversation/attachments/content'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type SetStateAction } from 'react'

import type { AgentMode, ChatRequestPayload } from '../../api/conversation/types'
import type { TodoGroup } from '../../api/conversation/taskTrace'
import { conversationErrorMessage } from '../../api/conversation/errors'
import type { AuthUser } from '../../api/auth/types'
import {
  Button,
  ErrorBoundary,
  useThemePreference,
  ViewTabs,
} from '../../components/ui'
import type { ToastHandler } from '../../components/ui/ToastViewport'
import { isTranslationKey, useI18n } from '../../i18n'
import {
  ApprovalCard,
  type ApprovalSubmissionDecision,
} from '../conversation/components/ApprovalCard'
import { Composer } from '../conversation/components/Composer'
import { PlanQuestionComposer } from '../conversation/components/PlanQuestionComposer'
import { PlanReviewCard } from '../conversation/components/PlanReviewCard'
import { parseComposerSubmission } from '../conversation/composerCommand'
import { useAttachments } from '../conversation/useAttachments'
import { ComposerModelPicker } from './components/ComposerModelPicker'
import { Sidebar } from './components/Sidebar'
import {
  ConversationViewport,
} from './components/ConversationViewport'
import { EmptyConversationBrand } from './components/EmptyConversation'
import { WorkspaceDialogs } from './components/WorkspaceDialogs'
import { WorkspaceHeader } from './components/WorkspaceHeader'
import { ScrollToBottomButton } from './components/ScrollToBottomButton'
import { SettingsDialog } from '../settings/SettingsDialog'
import { useWorkspaceNavigation } from './useWorkspaceNavigation'
import { useModelCatalog } from './useModelCatalog'
import { useWorkspaceHistory } from './useWorkspaceHistory'
import { useWorkspaceState } from './useWorkspaceState'
import { ConversationNavigator } from '../conversation/navigation/ConversationNavigator'
import { conversationTurns } from '../conversation/navigation/turns'
import { useConversationWidth } from '../conversation/width/useConversationWidth'
import { ConversationWidthHandles } from '../conversation/width/ConversationWidthHandles'
import { useConversationScroll } from './useConversationScroll'
import { useConversationTitles } from './useConversationTitles'
import { usePendingConversations } from './usePendingConversations'
import { useConversationManagement } from './useConversationManagement'
import { useConversationMessageWindow } from './useConversationMessageWindow'
import { useWorkspaceLayoutAnimation } from './useWorkspaceLayoutAnimation'
import { buildConversationDisplayEntries } from '../conversation/todoTrace/displayEntries'
import { TodoTraceLauncher } from '../conversation/todoTrace/components/TodoTraceLauncher'
import { TodoTraceDrawer } from '../conversation/todoTrace/components/TodoTraceDrawer'
import { useTodoTraceDrawer } from '../conversation/todoTrace/useTodoTraceDrawer'
import { ChainTraceView } from '../conversation/chainTrace/ChainTraceView'
import '../conversation/conversation.css'
import '../conversation/chainTrace/chainTrace.css'
import '../conversation/todoTrace/todoTrace.css'
import './workspace.css'
import {
  buildInitialPayload,
  buildPlanAbandonPayload,
  buildPlanResumePayload,
  buildPlanDismissPayload,
  buildResumePayload,
  prepareResumeSubmission,
} from '../conversation/agui'
import { useConversationStreamController } from '../conversation/stream/useConversationStreamController'
import {
  clearActiveRunSession,
  readActiveRunSession,
  readActiveRunSessions,
} from '../conversation/stream/activeRunSession'
import {
  buildEmptyConversation,
  selectCurrentConversation,
  updateConversation,
} from '../../lib/workspace'
import { readPageFromLocation, readThreadFromLocation, writeWorkspaceToLocation } from '../../lib/threadRoute'
import type {
  ApprovalState,
  Conversation,
  Message,
  PlanInteraction,
  PlanQuestionState,
  PlanReviewState,
} from '../../types'

interface PendingResume {
  kind: 'tool' | 'plan'
  threadId: string
  payload: ChatRequestPayload
  expectedInterruptIds: readonly string[]
}

const RESUME_RUN_DEDUPE_LIMIT = 256

const matchesApprovalGroup = (
  approval: ApprovalState | undefined,
  expectedInterruptIds: readonly string[],
) => Boolean(
  approval
  && approval.items.length === expectedInterruptIds.length
  && approval.items.every(
    (item, index) => item.interruptId === expectedInterruptIds[index],
  ),
)

const withFinalApprovalDecision = (
  approval: ApprovalState,
  finalDecision?: ApprovalSubmissionDecision,
) => {
  if (!finalDecision) return approval
  const activeIndex = approval.items.findIndex(
    (item) => item.interruptId === finalDecision.interruptId,
  )
  if (activeIndex < 0) return approval
  return {
    ...approval,
    activeIndex,
    mode: 'options' as const,
    error: undefined,
    items: approval.items.map((item, index) => index === activeIndex
      ? {
          ...item,
          decision: finalDecision.decision,
          rejectionReason: finalDecision.decision === 'rejected'
            ? finalDecision.rejectionReason
            : undefined,
        }
      : item),
  }
}

export function WorkspaceScreen({
  user,
  onLogout,
  onToast,
}: {
  user: AuthUser
  onLogout: () => void
  onToast: ToastHandler
}) {
  const { t } = useI18n()
  const {
    workspace,
    setWorkspace,
    retainConversationDetails,
    setComposerPreference,
    acknowledgeComposerPreferences,
  } = useWorkspaceState()
  useConversationTitles(workspace, setWorkspace)
  const pendingConversations = usePendingConversations()
  const selectPendingConversation = pendingConversations.select
  const [newSubmission, setNewSubmission] = useState<string | null>(null)
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null)
  const [textRevealProgress] = useState(() => new Map<string, string>())
  useEffect(() => {
    const conversations = draftConversation ? [...workspace.conversations, draftConversation] : workspace.conversations
    const retained = new Set(conversations.flatMap(conversation => conversation.messages.flatMap(
      message => message.liveText ? [message.liveText.key] : [],
    )))
    // 展示进度只随当前工作台中的消息保留，删除会话或释放历史窗口后同步清理
    for (const key of textRevealProgress.keys()) {
      if (!retained.has(key)) textRevealProgress.delete(key)
    }
  }, [workspace.conversations, draftConversation, textRevealProgress])
  const [draftModel, setDraftModel] = useState('')
  const [draftAccessMode, setDraftAccessMode] = useState<Conversation['accessMode']>('full')
  const [draft, setDraftValue] = useState('')
  const submissionLocks = useRef(new Map<string, string>())
  const draftRevision = useRef(0)
  const setDraft = useCallback((value: SetStateAction<string>) => {
    draftRevision.current += 1
    setDraftValue(value)
  }, [])
  const [isModelPickerOpen, setModelPickerOpen] = useState(false)
  const [pendingResume, setPendingResume] = useState<PendingResume | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [activePage, setActivePage] = useState(readPageFromLocation)
  const [automationModalOpen, setAutomationModalOpen] = useState(false)
  const [workspaceView, setWorkspaceView] = useState<'conversation' | 'trace'>('conversation')
  const settingsRestoreFocus = useRef<HTMLElement | null>(null)
  const theme = useThemePreference()
  const navigation = useWorkspaceNavigation()
  const {
    imageSupport,
    status: modelCatalogStatus,
    modelIds,
    defaultModelId,
    models,
    retry: retryModelCatalog,
  } = useModelCatalog()
  const localAttachments = useAttachments((message) => onToast(
    'error',
    isTranslationKey(message) ? t(message) : message,
  ))
  const appShell = useRef<HTMLDivElement>(null)
  const latestWorkspace = useRef(workspace)
  const startedResumeRunIds = useRef(new Set<string>())
  const [initialActiveSessions] = useState(readActiveRunSessions)
  const autoRecoveredRunIds = useRef(new Set<string>())
  const notifiedConversationEvents = useRef(new Set<string>())
  const notifiedImageSupportWarnings = useRef(new Set<string>())
  const notifiedChainWarnings = useRef(new Set<string>())
  latestWorkspace.current = workspace
  const pushToast = onToast
  const notifyConversation = useCallback((notice: NonNullable<Conversation['notice']>) => {
    if (notice.id?.endsWith(':terminal')) return
    const key = notice.id ?? `${notice.kind}:${notice.content}`
    if (notifiedConversationEvents.current.has(key)) return
    notifiedConversationEvents.current.add(key)
    if (notifiedConversationEvents.current.size > 256) {
      const oldest = notifiedConversationEvents.current.values().next().value
      if (oldest) notifiedConversationEvents.current.delete(oldest)
    }
    pushToast(notice.kind, notice.content)
  }, [pushToast])

  useEffect(() => {
    if (defaultModelId) setDraftModel((current) => current || defaultModelId)
  }, [defaultModelId])

  const {
    cancelRun,
    cancelPendingRunId,
    followDetachedConversation,
    releaseDraft,
    selectDraft,
    discardDraft,
    handoffTaskTraceFollow,
    isActiveThread,
    streamRun,
    recoverConversation,
  } = useConversationStreamController({
    workspace,
    setWorkspace,
    retainConversationDetails,
    acknowledgeComposerPreferences,
    setDraftConversation,
    onNotice: notifyConversation,
  })
  const {
    historyConversations,
    historyDayRanges,
    historyQuery,
    setHistoryQuery,
    isHistorySearchActive,
    isHistorySearching,
    historyCursor,
    isHistoryLoadingMore,
    historyLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    taskTraceLoadFailed,
    loadMoreHistory,
    retryHistoryLoad,
    retryHistoryBootstrap,
    hydrateConversation,
    retryTaskTrace,
    loadOlderTrace,
  } = useWorkspaceHistory({
    workspace,
    setWorkspace,
    retainConversationDetails,
    defaultModelId,
    modelCatalogStatus,
    refreshOnActivation: workspaceView === 'trace',
    followDetachedConversation,
    prepareTaskTraceOwner: handoffTaskTraceFollow,
    onToast: pushToast,
  })

  const selectedConversation = useMemo(
    () => workspace.conversations.find((item) => item.threadId === workspace.currentThreadId),
    [workspace],
  )
  const conversation = useMemo(() => {
    if (workspace.currentThreadId) {
      return selectedConversation
        ?? buildEmptyConversation({
          threadId: workspace.currentThreadId,
          now: new Date().toISOString(),
          model: draftModel,
        })
    }
    return draftConversation ?? buildEmptyConversation({ now: new Date().toISOString(), model: draftModel, accessMode: draftAccessMode })
  }, [draftConversation, draftModel, draftAccessMode, selectedConversation, workspace.currentThreadId])

  const notifyChainWarning = useCallback((message: string) => {
    const key = `${conversation.threadId}:${message}`
    if (notifiedChainWarnings.current.has(key)) return
    notifiedChainWarnings.current.add(key)
    if (notifiedChainWarnings.current.size > 256) {
      const oldest = notifiedChainWarnings.current.values().next().value
      if (oldest) notifiedChainWarnings.current.delete(oldest)
    }
    pushToast('warning', message)
  }, [conversation.threadId, pushToast])

  useEffect(() => {
    const imageAttachmentIds = localAttachments.attachments
      .filter((item) => item.kind === 'image')
      .map((item) => item.id)
      .join(',')
    const support = imageSupport(conversation.model)
    if (!imageAttachmentIds || modelCatalogStatus !== 'ready' || support === 'supported') return
    const key = `${conversation.threadId || 'draft'}:${conversation.model}:${support}:${imageAttachmentIds}`
    if (notifiedImageSupportWarnings.current.has(key)) return
    notifiedImageSupportWarnings.current.add(key)
    if (notifiedImageSupportWarnings.current.size > 256) {
      const oldest = notifiedImageSupportWarnings.current.values().next().value
      if (oldest) notifiedImageSupportWarnings.current.delete(oldest)
    }
    pushToast(
      'warning',
      support === 'unsupported' ? t('当前模型不支持图片') : t('当前模型的图片能力未确认'),
    )
  }, [conversation.model, conversation.threadId, imageSupport, localAttachments.attachments, modelCatalogStatus, pushToast, t])

  useEffect(() => {
    const notification = conversation.notice
    if (notification && !notification.id?.endsWith(':terminal')) notifyConversation(notification)
  }, [conversation, notifyConversation])

  useEffect(() => {
    if (activePage === 'automation') {
      document.title = `TinkerFin - ${t('自动化')}`
      return
    }
    document.title = conversation.threadId && conversation.title.trim()
      ? conversation.title.trim()
      : 'TinkerFin'
  }, [activePage, conversation.threadId, conversation.title, t])

  useEffect(() => () => {
    document.title = 'TinkerFin'
  }, [])

  const taskTraceBlocked = Boolean(
    (conversation.approval && !conversation.approval.submitted)
    || (conversation.planInteraction && !conversation.planInteraction.submitted)
    || conversation.pendingInteractionKind,
  )
  const isRunning = isConversationRunning(conversation)
  const {
    isFollowingLatest,
    resizeContent: resizeConversationContent,
    paneRef: conversationPane,
    messageEndRef: messageEnd,
    showScrollToBottom,
    fadeScrollToBottom,
    handleScroll: handleConversationScroll,
    scrollToBottomImmediately: scrollConversationToBottomImmediately,
    syncToBottomIfFollowing: syncConversationToBottomIfFollowing,
    markUserScrollIntent,
    pauseFollowing,
    scrollToBottom: scrollConversationToBottom,
    pauseScrollToBottomFade,
    resumeScrollToBottomFade,
    focusScrollToBottom,
    blurScrollToBottom,
  } = useConversationScroll({
    conversation,
    isRunning,
    active: activePage === 'conversation' && workspaceView === 'conversation',
  })
  const taskDrawerLayout = useDrawerLayout(640, resizeConversationContent)
  const traceDrawerLayout = useDrawerLayout(520)
  const taskDrawer = useTodoTraceDrawer({
    threadId: conversation.threadId,
    taskTrace: conversation.taskTrace,
    blocked: taskTraceBlocked,
    available: navigation.band === 'mobile' || taskDrawerLayout.available,
  })
  const taskDetailPageOpen = navigation.band === 'mobile' && taskDrawer.open
  const closeTaskDrawer = taskDrawer.close
  useWorkspaceLayoutAnimation({
    shellRef: appShell,
    layoutKey: `${navigation.mode}:${taskDrawer.open && !taskDetailPageOpen ? 'open' : 'closed'}`,
  })
  const conversationWidth = useConversationWidth(resizeConversationContent)
  const isConversationHydrating = Boolean(
    workspace.currentThreadId
    && selectedConversation
    && !selectedConversation.isHydrated
    && hydrationState?.threadId === workspace.currentThreadId
    && hydrationState.status === 'loading',
  )
  const isConversationHydrationFailed = Boolean(
    workspace.currentThreadId
    && selectedConversation
    && !selectedConversation.isHydrated
    && hydrationState?.threadId === workspace.currentThreadId
    && hydrationState.status === 'failed',
  )
  const isInitialHistoryUnavailable = historyBootstrapStatus === 'error'
    && workspace.conversations.length === 0
    && !draftConversation
  const showConversationHero = conversation.messages.length === 0
    && !conversation.notice
    && isHistoryBootstrapped
    && historyBootstrapStatus === 'ready'
    && !isConversationHydrating
    && !isConversationHydrationFailed
    && !isInitialHistoryUnavailable
  const updateCurrent = useCallback((updater: (item: Conversation) => Conversation) => {
    setWorkspace((state) => updateConversation(state, state.currentThreadId, updater))
  }, [setWorkspace])

  useLayoutEffect(() => {
    const shell = appShell.current
    const composer = shell?.querySelector<HTMLElement>('.composer-dock')
    if (!shell || !composer) return
    const measure = () => {
      shell.style.setProperty(
        '--composer-height',
        `${Math.max(80, composer.getBoundingClientRect().height)}px`,
      )
      // Composer 改变可视高度时只跟随仍停留在底部的会话
      syncConversationToBottomIfFollowing()
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(composer)
    return () => observer.disconnect()
  }, [activePage, syncConversationToBottomIfFollowing, workspaceView])

  // 页面与会话统一写入地址，自动化页中的后台会话更新不会带入 thread 参数
  useEffect(() => {
    if (!isHistoryBootstrapped) return
    writeWorkspaceToLocation(activePage, workspace.currentThreadId)
  }, [activePage, isHistoryBootstrapped, workspace.currentThreadId])

  // 历史导航释放草稿的页面选择权，迟到的首帧只能更新原会话
  useEffect(() => {
    const onPopState = () => {
      selectPendingConversation(null)
      submissionLocks.current.delete('')
      releaseDraft()
      const threadId = readThreadFromLocation()
      setActivePage(readPageFromLocation())
      setWorkspaceView('conversation')
      setWorkspace((state) =>
        state.currentThreadId === threadId
          ? state
          : !threadId || state.conversations.some((item) => item.threadId === threadId)
            ? selectCurrentConversation(state, threadId)
            : state,
      )
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [releaseDraft, selectPendingConversation, setWorkspace])

  useEffect(() => {
    if (!workspace.currentThreadId || !selectedConversation?.isHydrated) return
    const active = readActiveRunSession(selectedConversation.threadId)
    if (!active || (active.threadId && active.threadId !== selectedConversation.threadId)) return
    const runMatches = selectedConversation.activeRunId === active.payload.runId
    if (selectedConversation.runStatus !== 'detached' || !runMatches) {
      if (active.threadId === selectedConversation.threadId && selectedConversation.runStatus !== 'streaming') {
        clearActiveRunSession(active.payload.runId)
      }
      return
    }
    if (isActiveThread(selectedConversation.threadId)) return
    // 仅恢复进入页面时留下的连接；本页失联后的恢复必须由用户明确发起
    if (!initialActiveSessions.some((item) => item.payload.runId === active.payload.runId)
      || autoRecoveredRunIds.current.has(active.payload.runId)) return
    autoRecoveredRunIds.current.add(active.payload.runId)

    void recoverConversation(selectedConversation.threadId)
  }, [
    isActiveThread,
    initialActiveSessions,
    selectedConversation?.activeRunId,
    selectedConversation?.isHydrated,
    selectedConversation?.lastSeq,
    selectedConversation?.runStatus,
    selectedConversation?.threadId,
    recoverConversation,
    workspace.currentThreadId,
  ])

  const childToolsByRunId = useMemo(() => {
    const grouped = new Map<string, Conversation['messages']>()
    for (const message of conversation.messages) {
      if (message.role !== 'tool' || !message.meta?.sourceAgentName || !message.meta.runId) continue
      const tools = grouped.get(message.meta.runId) ?? []
      tools.push(message)
      grouped.set(message.meta.runId, tools)
    }
    return grouped
  }, [conversation.messages])

  const displayMessages = useMemo(
    () => buildConversationDisplayEntries(conversation),
    [conversation],
  )

  const [directoryThreadId, setDirectoryThreadId] = useState<string | null>(null)
  const rawNavigationTurns = useMemo(() => conversationTurns(conversation.messages), [conversation.messages])
  const navigationTurnsRef = useRef(rawNavigationTurns)
  if (rawNavigationTurns.length !== navigationTurnsRef.current.length || rawNavigationTurns.some((turn, index) => {
    const previous = navigationTurnsRef.current[index]
    return turn.messageId !== previous.messageId || turn.prompt !== previous.prompt || turn.response !== previous.response
  })) navigationTurnsRef.current = rawNavigationTurns
  const navigationTurns = navigationTurnsRef.current
  useEffect(() => { setDirectoryThreadId(null) }, [conversation.threadId, workspaceView])
  useEffect(() => {
    if (navigationTurns.length < 2 || isConversationHydrating || isConversationHydrationFailed) setDirectoryThreadId(null)
  }, [navigationTurns.length, isConversationHydrating, isConversationHydrationFailed])
  const directoryOpen = directoryThreadId === conversation.threadId && workspaceView === 'conversation'
  const changeDirectoryOpen = useCallback((open: boolean) => {
    setDirectoryThreadId(open ? conversation.threadId : null)
  }, [conversation.threadId])

  const messageWindow = useConversationMessageWindow({
    threadId: conversation.threadId,
    entries: displayMessages,
    active: activePage === 'conversation' && workspaceView === 'conversation',
    historyCursor: conversation.trace?.historyCursor,
    paneRef: conversationPane,
    loadOlderTrace,
    readingPosition: {
      isHydrated: Boolean(conversation.isHydrated),
      threadIds: workspace.conversations.map(item => item.threadId),
      isFollowingLatest,
      pauseFollowing,
      scrollToBottom: scrollConversationToBottomImmediately,
      onMissing: () => pushToast('info', t('原阅读位置已不可用，已回到最新消息')),
    },
  })
  const navigationScope = useRef('')
  navigationScope.current = `${conversation.threadId}:${workspaceView}`
  const { revealMessage } = messageWindow
  const navigateToQuestion = useCallback(async (messageId: string) => {
    pauseFollowing()
    const scope = navigationScope.current
    try {
      const result = await revealMessage(messageId, 'start')
      if (scope !== navigationScope.current) return
      if (result === 'not-found') pushToast('error', t('未找到对应的提问'))
      else if (result === 'failed') pushToast('error', t('定位消息失败，请重试'))
    } catch {
      if (scope !== navigationScope.current) return
      pushToast('error', t('定位消息失败，请重试'))
    }
  }, [pauseFollowing, pushToast, revealMessage, t])

  const returnToLatestMessages = useCallback(() => {
    messageWindow.restoreTail()
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(scrollConversationToBottom)
    })
  }, [messageWindow, scrollConversationToBottom])

  const beginSend = useCallback((content: string, modeOverride?: AgentMode, resubmission?: Message) => {
    const trimmed = content.trim()
    const readyAttachments = resubmission ? resubmission.attachments ?? [] : localAttachments.attachments.flatMap(item => item.attachment ? [item.attachment] : [])
    const submissionKey = workspace.currentThreadId
    if (submissionLocks.current.has(submissionKey) || (submissionKey && isActiveThread(submissionKey))) return
    if ((!trimmed && !readyAttachments.length) || isRunning || !conversation.model || (!resubmission && localAttachments.attachments.some(item => item.state !== 'ready'))) return
    if (!conversation.threadId && conversation.runStatus === 'detached') return
    if (resubmission && (conversation.runStatus === 'detached' || conversation.pendingInteractionKind || conversation.approval || conversation.planInteraction)) return
    if (readyAttachments.some(item => item.mime_type.startsWith('image/')) && imageSupport(conversation.model) !== 'supported') {
      if (resubmission) {
        const support = imageSupport(conversation.model)
        pushToast(
          'warning',
          support === 'unsupported' ? t('当前模型不支持图片') : t('当前模型的图片能力未确认'),
        )
      }
      return
    }
    const submittedIds = localAttachments.attachments.map(item => item.id)
    const submittedThreadId = workspace.currentThreadId
    const submittedDraft = draft
    const clearedDraftRevision = draftRevision.current + 1
    const releaseSubmission = (runId: string) => {
      // 旧请求只能释放自己的提交入口，不能影响随后开始的新草稿
      if (submissionLocks.current.get(submissionKey) === runId) {
        submissionLocks.current.delete(submissionKey)
      }
    }
    const onAccepted = () => {
      if (!resubmission) localAttachments.completeSend(submittedIds)
    }
    const onRequestRejected = () => {
      if (!resubmission && draftRevision.current === clearedDraftRevision
        && latestWorkspace.current.currentThreadId === submittedThreadId) {
        setDraft(submittedDraft)
      }
    }
    messageWindow.restoreTail()
    const effectiveMode = modeOverride ?? conversation.mode

    const now = new Date().toISOString()
    if (!workspace.currentThreadId) {
      const nextConversation = buildEmptyConversation({
        now,
        model: draftConversation?.model ?? draftModel,
        mode: effectiveMode,
        accessMode: conversation.accessMode,
      })
      const payload = buildInitialPayload(nextConversation, trimmed, readyAttachments)
      const requestMessage = payload.messages.at(0)
      if (!requestMessage) return
      const seededConversation: Conversation = {
        ...nextConversation,
        title: Array.from(trimmed).slice(0, 16).join('') || t('附件提问'),
        messages: [{
          id: requestMessage.id,
          role: 'user',
          content: messageText(requestMessage.content),
          attachments: messageAttachments(requestMessage.content),
          createdAt: now,
          meta: { runId: payload.runId },
        }],
        activeRunId: payload.runId,
        runStatus: 'streaming',
        historySynchronized: false,
        notice: undefined,
        approval: undefined,
        todos: [],
        taskTrace: { phase: 'ready', snapshot: { status: 'ready', todoGroups: [] } },
        serverState: {},
      }

      submissionLocks.current.set(submissionKey, payload.runId)
      scrollConversationToBottomImmediately()
      if (pendingConversations.selectedRunId) {
        discardDraft(pendingConversations.selectedRunId)
        pendingConversations.remove(pendingConversations.selectedRunId)
      }
      pendingConversations.select(payload.runId)
      setHistoryQuery('')
      setNewSubmission(payload.runId)
      setDraftConversation(seededConversation)
      if (!resubmission) setDraft('')
      void streamRun(nextConversation.threadId, payload, 'start', {
        target: 'draft',
        initialConversation: seededConversation,
        onDraftChange: next => pendingConversations.update(payload.runId, next),
        onRegistered: () => pendingConversations.remove(payload.runId),
        onAccepted: () => {
          // 已受理的运行按正式会话隔离，空白入口可继续创建新会话
          releaseSubmission(payload.runId)
          onAccepted()
        },
        onRequestRejected,
      }).finally(() => releaseSubmission(payload.runId))
      return
    }

    const currentConversation = workspace.conversations.find((item) => item.threadId === workspace.currentThreadId)
    if (!currentConversation) return
    if (!currentConversation.isHydrated) {
      void hydrateConversation(currentConversation.threadId)
      return
    }
    if (currentConversation.runStatus === 'waiting_approval') {
      setWorkspace((state) => updateConversation(state, currentConversation.threadId, (item) => ({
        ...item,
        approval: item.approval ? { ...item.approval, error: t('请先处理当前审批后再发送新消息') } : item.approval,
        planInteraction: item.planInteraction
          ? { ...item.planInteraction, error: t('请先处理当前 Plan 请求后再发送新消息') }
          : item.planInteraction,
      })))
      return
    }

    const sendingConversation = currentConversation.mode === effectiveMode
      ? currentConversation
      : { ...currentConversation, mode: effectiveMode }
    const payload = buildInitialPayload(sendingConversation, trimmed, readyAttachments)
    const requestMessage = payload.messages.at(0)
    if (!requestMessage) return
    if (modeOverride) setComposerPreference(currentConversation.threadId, { mode: modeOverride })
    submissionLocks.current.set(submissionKey, payload.runId)
    scrollConversationToBottomImmediately()
    setWorkspace((state) => {
      return updateConversation(state, currentConversation.threadId, (item) => ({
        ...item,
        mode: effectiveMode,
        updatedAt: now,
        activeRunId: payload.runId,
        runStatus: 'streaming',
        historySynchronized: false,
        notice: undefined,
        approval: undefined,
        todos: [],
        serverState: {},
        messages: [...item.messages, {
          id: requestMessage.id,
          role: 'user',
          content: messageText(requestMessage.content),
          attachments: messageAttachments(requestMessage.content),
          createdAt: now,
          meta: { runId: payload.runId },
        }],
      }))
    })
    if (!resubmission) setDraft('')
    void streamRun(currentConversation.threadId, payload, 'start', { target: 'workspace', onAccepted, onRequestRejected }).finally(() => releaseSubmission(payload.runId))
  }, [isActiveThread, pushToast, t, conversation, localAttachments, imageSupport, draft, draftConversation?.model, draftModel, pendingConversations, discardDraft, setHistoryQuery, hydrateConversation, isRunning, messageWindow, scrollConversationToBottomImmediately, setDraft, setComposerPreference, setWorkspace, streamRun, workspace.conversations, workspace.currentThreadId])

  const retryRun = useCallback((message: Message) => {
    const current = latestWorkspace.current.conversations.find(item => item.threadId === workspace.currentThreadId)
    const original = current?.messages.find(item => item.id === message.id && item.role === 'user')
    if (!current?.isHydrated || !original || original.meta?.contentOmitted) return
    if (!current.runFailures?.some(item => item.runId === original.meta?.runId && item.retryable)) return
    beginSend(original.content, undefined, original)
  }, [beginSend, workspace.currentThreadId])

  useEffect(() => {
    if (!pendingResume) return
    const claimedConversation = workspace.conversations.find(
      (item) => item.threadId === pendingResume.threadId,
    )
    const isExpectedGroup = pendingResume.kind === 'tool'
      ? claimedConversation?.approval?.items.length === pendingResume.expectedInterruptIds.length
        && claimedConversation.approval.items.every(
          (item, index) => item.interruptId === pendingResume.expectedInterruptIds[index],
        )
      : claimedConversation?.planInteraction?.interruptId === pendingResume.expectedInterruptIds[0]
    const isClaimed = claimedConversation?.runStatus === 'streaming'
      && claimedConversation.activeRunId === pendingResume.payload.runId
      && (pendingResume.kind === 'tool'
        ? claimedConversation.approval?.submitted === true
        : claimedConversation.planInteraction?.submitted === true)
      && isExpectedGroup
    setPendingResume(null)
    if (!isClaimed || startedResumeRunIds.current.has(pendingResume.payload.runId)) return

    startedResumeRunIds.current.add(pendingResume.payload.runId)
    while (startedResumeRunIds.current.size > RESUME_RUN_DEDUPE_LIMIT) {
      const oldest = startedResumeRunIds.current.values().next().value
      if (typeof oldest !== 'string') break
      startedResumeRunIds.current.delete(oldest)
    }
    // 同一个 resume runId 在当前页面生命周期内只能启动一次；流结束时不能删除，
    // 否则仍携带旧 pendingResume 的并发渲染会再次提交已结算审批
    void streamRun(
      pendingResume.threadId,
      pendingResume.payload,
      'resume',
    )
  }, [pendingResume, streamRun, workspace.conversations])

  const submitApproval = useCallback((
    expectedInterruptIds: readonly string[],
    finalDecision?: ApprovalSubmissionDecision,
  ) => {
    const authoritativeConversation = latestWorkspace.current.conversations.find(
      (item) => item.threadId === conversation.threadId,
    )
    if (
      !authoritativeConversation?.approval
      || authoritativeConversation.runStatus === 'streaming'
      || !workspace.currentThreadId
    ) return
    if (!matchesApprovalGroup(authoritativeConversation.approval, expectedInterruptIds)) {
      updateCurrent((item) => ({
        ...item,
        historySynchronized: false,
        approval: item.approval
          ? { ...item.approval, error: t('当前审批已更新，请重新检查') }
          : item.approval,
      }))
      return
    }
    const completedApproval = withFinalApprovalDecision(
      authoritativeConversation.approval,
      finalDecision,
    )
    const completedConversation = {
      ...authoritativeConversation,
      approval: completedApproval,
    }
    const incomplete = completedApproval.items.some((item) => !item.decision)
    if (incomplete) {
      updateCurrent((item) => ({
        ...item,
        historySynchronized: false,
        approval: item.approval ? { ...item.approval, error: t('请先处理所有待审批项') } : item.approval,
      }))
      return
    }

    let payload: ChatRequestPayload
    try {
      payload = buildResumePayload(
        completedConversation,
        expectedInterruptIds,
      )
    } catch (error) {
      updateCurrent((item) => ({
        ...item,
        historySynchronized: false,
        approval: item.approval
          ? matchesApprovalGroup(item.approval, expectedInterruptIds)
            ? {
              ...completedApproval,
              error: conversationErrorMessage(error, 'approval_stale'),
            }
            : item.approval
          : item.approval,
      }))
      return
    }
    setWorkspace((state) => updateConversation(
      state,
      authoritativeConversation.threadId,
      (item) => {
        if (!matchesApprovalGroup(item.approval, expectedInterruptIds)) return item
        const prepared = prepareResumeSubmission(
          { ...item, approval: completedApproval },
          expectedInterruptIds,
        )
        return prepared === item
          ? item
          : { ...prepared, activeRunId: payload.runId, historySynchronized: false }
      },
    ))
    setPendingResume({
      kind: 'tool',
      threadId: authoritativeConversation.threadId,
      payload,
      expectedInterruptIds: [...expectedInterruptIds],
    })
  }, [conversation.threadId, setWorkspace, t, updateCurrent, workspace.currentThreadId])

  const submitPlanInteraction = useCallback((
    reviewAction?: 'approve' | 'reject' | 'cancel' | 'dismiss',
  ) => {
    const authoritative = latestWorkspace.current.conversations.find(
      (item) => item.threadId === conversation.threadId,
    )
    if (!authoritative?.planInteraction || authoritative.runStatus === 'streaming') return
    const requested = reviewAction && reviewAction !== 'dismiss' && authoritative.planInteraction.kind === 'review'
      ? {
          ...authoritative,
          planInteraction: {
            ...authoritative.planInteraction,
            action: reviewAction,
          },
        }
      : authoritative
    let payload: ChatRequestPayload
    try {
      payload = reviewAction === 'dismiss' ? buildPlanDismissPayload(authoritative) : buildPlanResumePayload(requested)
    } catch (error) {
      const message = conversationErrorMessage(error, 'plan_submit_failed')
      updateCurrent((item) => ({
        ...item,
        historySynchronized: false,
        planInteraction: item.planInteraction
          ? { ...item.planInteraction, error: message }
          : item.planInteraction,
      }))
      return
    }
    const interruptId = authoritative.planInteraction.interruptId
    setWorkspace((state) => updateConversation(state, authoritative.threadId, (item) => ({
      ...item,
      runStatus: 'streaming',
      historySynchronized: false,
      activeRunId: payload.runId,
      planInteraction: item.planInteraction
        ? {
            ...item.planInteraction,
            ...(reviewAction && reviewAction !== 'dismiss' && item.planInteraction.kind === 'review'
              ? { action: reviewAction }
              : {}),
            submitted: true,
            error: undefined,
          }
        : item.planInteraction,
    })))
    setPendingResume({
      kind: 'plan',
      threadId: authoritative.threadId,
      payload,
      expectedInterruptIds: [interruptId],
    })
  }, [conversation.threadId, setWorkspace, updateCurrent])

  const abandonPlanInteraction = useCallback((threadId: string) => {
    const authoritative = latestWorkspace.current.conversations.find(
      (item) => item.threadId === threadId,
    )
    if (!authoritative?.planInteraction || authoritative.runStatus === 'streaming') return
    let payload: ChatRequestPayload
    try {
      payload = buildPlanAbandonPayload(authoritative)
    } catch (error) {
      updateCurrent((item) => ({
        ...item,
        historySynchronized: false,
        planInteraction: item.planInteraction
          ? {
              ...item.planInteraction,
              error: conversationErrorMessage(error, 'plan_submit_failed'),
            }
          : item.planInteraction,
      }))
      return
    }
    const interruptId = authoritative.planInteraction.interruptId
    setWorkspace((state) => updateConversation(state, threadId, (item) => ({
      ...item,
      mode: 'default',
      runStatus: 'streaming',
      historySynchronized: false,
      activeRunId: payload.runId,
      planInteraction: item.planInteraction
        ? { ...item.planInteraction, submitted: true, error: undefined }
        : item.planInteraction,
    })))
    setPendingResume({
      kind: 'plan',
      threadId,
      payload,
      expectedInterruptIds: [interruptId],
    })
  }, [setWorkspace, updateCurrent])

  const changeApproval = useCallback((
    threadId: string,
    updater: (approval: ApprovalState) => ApprovalState,
  ) => {
    setWorkspace((state) => updateConversation(state, threadId, (item) => (
      item.approval
        ? { ...item, approval: updater(item.approval), historySynchronized: false }
        : item
    )))
  }, [setWorkspace])

  const changePlanInteraction = useCallback((
    threadId: string,
    updater: (interaction: PlanInteraction) => PlanInteraction,
  ) => {
    setWorkspace((state) => updateConversation(state, threadId, (item) => (
      item.planInteraction
        ? { ...item, planInteraction: updater(item.planInteraction), historySynchronized: false }
        : item
    )))
  }, [setWorkspace])

  const {
    dialog,
    dialogPending,
    closeDialog,
    confirmDialog,
    selectConversation,
    newConversation,
    pinConversation,
    pinPendingThreadIds,
    renameConversation,
    deleteConversation,
    requestDisablePlan,
  } = useConversationManagement({
    workspace,
    conversation,
    setWorkspace,
    setDraft,
    setDraftConversation,
    setDraftModel,
    followDetachedConversation,
    abandonPlanInteraction,
    cancelRun,
    isActiveThread,
    onToast: pushToast,
    onConversationBoundary: () => {
      messageWindow.captureReadingPosition()
      setActivePage('conversation')
      pendingConversations.select(null)
      submissionLocks.current.delete('')
      releaseDraft()
      draftRevision.current += 1
      localAttachments.clearAttachments()
      setWorkspaceView('conversation')
    },
  })

  const setAgentMode = useCallback((mode: AgentMode) => {
    if (mode === conversation.mode) return
    if (!workspace.currentThreadId) {
      setDraftConversation((current) => ({ ...(current ?? conversation), mode }))
      return
    }
    setComposerPreference(workspace.currentThreadId, { mode })
  }, [conversation, setComposerPreference, workspace.currentThreadId])

  const exitPlanMode = useCallback(() => {
    if (isRunning || conversation.mode !== 'plan') return
    if (conversation.planInteraction) {
      requestDisablePlan(conversation.threadId)
      return
    }
    setAgentMode('default')
    pushToast('info', t('已关闭 Plan'))
  }, [conversation.mode, conversation.planInteraction, conversation.threadId, isRunning, pushToast, requestDisablePlan, setAgentMode, t])

  const stop = async () => {
    if (!isRunning || !conversation.activeRunId) return
    try {
      const cancelled = await cancelRun(conversation.threadId)
      pushToast('info', cancelled ? t('任务已停止') : t('任务已经结束'))
    } catch {
      pushToast('error', t('停止任务失败，请重试'))
    }
  }

  const selectAccessMode = (accessMode: Conversation['accessMode']) => {
    if (!workspace.currentThreadId) {
      setDraftAccessMode(accessMode)
      setDraftConversation(current => current ? { ...current, accessMode } : current)
    } else setComposerPreference(workspace.currentThreadId, { accessMode })
  }

  const selectModel = (model: string) => {
    if (!workspace.currentThreadId) {
      setDraftModel(model)
      setDraftConversation((current) => (current ? { ...current, model } : current))
      setModelPickerOpen(false)
      return
    }
    setComposerPreference(workspace.currentThreadId, { model })
    setModelPickerOpen(false)
  }

  const send = () => {
    const submission = parseComposerSubmission(draft)
    if (submission.kind === 'plan-off-unsupported') {
      pushToast('info', t('请点击输入框中的 Plan 按钮关闭'))
      return
    }
    if (submission.kind === 'plan-enable') {
      setAgentMode('plan')
      setDraft('')
      pushToast('info', conversation.mode === 'plan' ? t('Plan 已开启') : t('已开启 Plan'))
      return
    }
    if (submission.kind === 'plan-message') {
      beginSend(submission.content, 'plan')
      return
    }
    beginSend(submission.content)
  }

  const locateTodoGroup = useCallback((group: TodoGroup) => {
    if (taskDetailPageOpen) closeTaskDrawer(false)
    const locate = async () => {
      const result = await messageWindow.revealMessage(group.userMessageId)
      if (result === 'failed') pushToast('error', t('定位消息失败，请重试'))
      else if (result === 'not-found') pushToast('info', t('未找到任务对应的用户消息'))
    }
    void locate()
  }, [messageWindow, pushToast, t, taskDetailPageOpen, closeTaskDrawer])

  // Portal 对话框打开时整块工作区退出辅助技术与键盘路径，只保留最上层操作
  const portalModalActive = settingsOpen || dialog != null || directoryOpen || automationModalOpen
  const taskTraceLauncher = taskTraceBlocked ? undefined : (
    <TodoTraceLauncher
      ref={taskDrawer.launcherRef}
      taskTrace={conversation.taskTrace}
      open={taskDrawer.open}
      loadFailed={taskTraceLoadFailed}
      onToggle={() => {
        taskDrawer.toggle()
        if (navigation.band !== 'mobile' && !taskDrawerLayout.available) pushToast('info', t('展开窗口后可查看'))
      }}
      onRetry={() => retryTaskTrace(conversation.threadId)}
    />
  )
  const selectWorkspaceView = (view: 'conversation' | 'trace') => {
    messageWindow.captureReadingPosition()
    if (view === 'trace') {
      if (!conversation.threadId) return
      taskDrawer.close(false)
    }
    setWorkspaceView(view)
  }

  return (
    <TextRevealProgressContext.Provider value={textRevealProgress}>
    <AttachmentReferenceContext.Provider value={localAttachments.addReference}>
    <div
      ref={appShell}
      className={`app-shell ${taskDrawer.open ? 'has-todo-trace' : ''}`}
      id="top"
      data-sidebar-mode={navigation.mode}
      aria-hidden={portalModalActive || undefined}
      inert={portalModalActive || undefined}
    >
      <Sidebar
        workspace={workspace}
        historyConversations={historyConversations}
        pendingConversations={pendingConversations.items}
        selectedPendingRunId={pendingConversations.selectedRunId}
        newSubmission={newSubmission}
        onSelectPending={(runId) => {
          const pending = pendingConversations.items.find(item => item.runId === runId)
          if (!pending) return
          newConversation()
          pendingConversations.select(runId)
          selectDraft(runId)
          setDraftConversation(pending.conversation)
        }}
        onRemovePending={(runId) => {
          discardDraft(runId)
          pendingConversations.remove(runId)
          if (pendingConversations.selectedRunId === runId) newConversation()
        }}
        historyDayRanges={historyDayRanges}
        historyQuery={historyQuery}
        onHistoryQueryChange={setHistoryQuery}
        isHistorySearchActive={isHistorySearchActive}
        isHistorySearching={isHistorySearching}
        mode={navigation.mode}
        settledMode={navigation.settledMode}
        overlayOpen={navigation.overlayOpen}
        wideInteractive={navigation.wideInteractive}
        railInteractive={navigation.railInteractive}
        onToggleMode={navigation.toggleDesktopMode}
        onRequestExpanded={navigation.requestExpanded}
        onCloseOverlay={navigation.closeOverlay}
        automationActive={activePage === 'automation'}
        onOpenAutomation={() => {
          messageWindow.captureReadingPosition()
          taskDrawer.close(false)
          changeDirectoryOpen(false)
          navigation.closeOverlay(false)
          setActivePage('automation')
        }}
        onNew={() => {
          setActivePage('conversation')
          setWorkspaceView('conversation')
          newConversation()
        }}
        onSelect={(threadId) => {
          setActivePage('conversation')
          selectConversation(threadId)
        }}
        onPin={pinConversation}
        pinPendingThreadIds={pinPendingThreadIds}
        onRename={renameConversation}
        onDelete={deleteConversation}
        hasMore={historyCursor != null}
        onLoadMore={loadMoreHistory}
        isLoadingMore={isHistoryLoadingMore}
        loadMoreError={historyLoadError ?? undefined}
        onRetryLoadMore={retryHistoryLoad}
        user={user}
        onOpenSettings={(restoreFocusTo) => {
          taskDrawer.close(false)
          settingsRestoreFocus.current = restoreFocusTo ?? null
          setSettingsOpen(true)
        }}
        onLogout={onLogout}
        backgroundInert={portalModalActive}
      />
      <div ref={taskDrawerLayout.hostRef} className={`workspace-content${taskDetailPageOpen ? ' has-task-detail-page' : ''}`} style={{ '--layout-drawer-width': `${taskDrawerLayout.width}px` } as CSSProperties}>
      <main
        ref={conversationWidth.rootRef}
        data-workspace-layout-target="main"
        id="main-content"
        className="workspace-main"
        aria-hidden={taskDetailPageOpen || (navigation.mode === 'overlay' && navigation.overlayOpen) || undefined}
        inert={taskDetailPageOpen || (navigation.mode === 'overlay' && navigation.overlayOpen) || undefined}
      >
        {activePage === 'automation' ? (
          <ErrorBoundary onError={() => pushToast('error', t('自动化区域无法显示'))}
            fallback={({ reset }) => <div className="automation-empty"><p>{t('自动化区域无法显示')}</p><Button type="button" onClick={reset}>{t('重新加载')}</Button></div>}>
            <AutomationPage navigationTriggerRef={navigation.overlayTriggerRef} onOpenNavigation={navigation.openOverlay}
              onModalChange={setAutomationModalOpen} onToast={pushToast} defaultModelId={defaultModelId}
              renderModelChoice={(model, onSelect) => <ComposerModelPicker model={model} models={models} defaultModelId={defaultModelId}
                status={modelCatalogStatus} open={isModelPickerOpen} onOpenChange={setModelPickerOpen}
                onSelectModel={value => { onSelect(value); setModelPickerOpen(false) }} onRetry={retryModelCatalog} />} />
          </ErrorBoundary>
        ) : <>
        <WorkspaceHeader
          conversationTitle={conversation.title}
          overlayTriggerRef={navigation.overlayTriggerRef}
          onOpenOverlay={navigation.openOverlay}
          navigation={conversation.threadId ? (
            <ViewTabs
              value={workspaceView}
              label={t('会话视图')}
              options={[
                { value: 'conversation', label: t('对话'), controls: 'conversation-panel' },
                {
                  value: 'trace',
                  label: t('链路'),
                  controls: 'chain-trace-panel',
                  disabled: !conversation.threadId,
                },
              ]}
              onChange={selectWorkspaceView}
              className="workspace-view-tabs"
            />
          ) : undefined}
        />
        {workspaceView === 'conversation' ? (
          <>
            <ConversationViewport
              widthHandles={<ConversationWidthHandles control={conversationWidth} />}
              conversation={conversation}
              entries={messageWindow.visibleEntries}
              navigation={!isConversationHydrating && !isConversationHydrationFailed
                && isHistoryBootstrapped && !isInitialHistoryUnavailable && navigationTurns.length >= 2 ? (
                  <ConversationNavigator key={conversation.threadId} turns={navigationTurns}
                    paneRef={conversationPane} open={directoryOpen} onOpenChange={changeDirectoryOpen}
                    onNavigate={navigateToQuestion} />
                ) : undefined}
              hasEarlierMessages={messageWindow.hasEarlierMessages}
              childToolsByRunId={childToolsByRunId}
              paneRef={conversationPane}
              messageEndRef={messageEnd}
              historyStatus={historyBootstrapStatus}
              isHistoryBootstrapped={isHistoryBootstrapped}
              isInitialHistoryUnavailable={isInitialHistoryUnavailable}
              isHydrating={isConversationHydrating}
              isHydrationFailed={isConversationHydrationFailed}
              isRunning={isRunning}
              onScroll={(pane) => {
                handleConversationScroll(pane)
                messageWindow.captureReadingPosition()
              }}
              onUserScrollIntent={() => {
                messageWindow.cancelReadingRestore()
                markUserScrollIntent()
              }}
              onRetryReadingPosition={messageWindow.readingPositionFailed
                ? () => void messageWindow.retryReadingPosition()
                : undefined}
              onRetryHistory={retryHistoryBootstrap}
              onRetryHydration={() => void hydrateConversation(conversation.threadId)}
              onError={(message) => pushToast('error', message)}
              onRecoverConversation={() => void recoverConversation(conversation.threadId, pendingConversations.selectedRunId ?? undefined)}
              onRetryRun={retryRun}
              retryDisabled={isRunning || conversation.runStatus === 'detached' || !conversation.isHydrated || !modelIds.includes(conversation.model) || Boolean(conversation.pendingInteractionKind || conversation.approval || conversation.planInteraction)}
              onLoadEarlierMessages={(trigger) => void messageWindow.loadEarlierMessages(trigger)}
            />
            <Composer
              value={draft}
              isRunning={isRunning}
              canStop={Boolean(conversation.threadId)}
              stopPending={cancelPendingRunId === conversation.activeRunId}
              isHydrating={isConversationHydrating}
              hero={showConversationHero ? <EmptyConversationBrand /> : undefined}
              takeover={conversation.approval && !conversation.approval.submitted
                ? (
                  <ApprovalCard
                    key={`${conversation.threadId}:${conversation.approval.items[0]?.interruptId ?? ''}`}
                    conversation={conversation}
                    onChange={(updater) => changeApproval(conversation.threadId, updater)}
                    onSubmit={submitApproval}
                  />
                )
                : conversation.planInteraction?.kind === 'questions'
                  ? (
                    <PlanQuestionComposer
                      threadId={conversation.threadId}
                      interaction={conversation.planInteraction}
                      onChange={(updater) => changePlanInteraction(
                        conversation.threadId,
                        (current) => current.kind === 'questions'
                          ? updater(current as PlanQuestionState)
                          : current,
                      )}
                      onSubmit={submitPlanInteraction}
                      onClose={() => submitPlanInteraction('dismiss')}
                    />
                  )
                  : conversation.planInteraction?.kind === 'review'
                    ? (
                      <PlanReviewCard
                        key={`${conversation.threadId}:${conversation.planInteraction.interruptId}`}
                        interaction={conversation.planInteraction}
                        onChange={(updater) => changePlanInteraction(
                          conversation.threadId,
                          (current) => current.kind === 'review'
                            ? updater(current as PlanReviewState)
                            : current,
                        )}
                        onSubmit={(action) => submitPlanInteraction(action)}
                        onClose={() => submitPlanInteraction('dismiss')}
                      />
                    )
                    : undefined}
              scrollToBottomControl={(showScrollToBottom || !messageWindow.followsTail)
                ? (
                  <ScrollToBottomButton
                    fading={fadeScrollToBottom}
                    onPointerEnter={pauseScrollToBottomFade}
                    onPointerLeave={resumeScrollToBottomFade}
                    onFocus={focusScrollToBottom}
                    onBlur={blurScrollToBottom}
                    onClick={returnToLatestMessages}
                  />
                )
                : undefined}
              taskTraceControl={taskTraceLauncher}
              accessControl={<AccessModePicker value={conversation.accessMode} onChange={selectAccessMode}
                disabled={isRunning || Boolean(conversation.approval || conversation.planInteraction) || isConversationHydrating} />}
              modelControl={(
                <ComposerModelPicker
                  model={conversation.model}
                  models={models}
                  defaultModelId={defaultModelId}
                  status={modelCatalogStatus}
                  open={isModelPickerOpen}
                  onOpenChange={setModelPickerOpen}
                  onSelectModel={selectModel}
                  onRetry={retryModelCatalog}
                />
              )}
              planActive={conversation.mode === 'plan'}
              planLocked={isRunning}
              attachments={localAttachments.attachments}
              attachmentBlocked={localAttachments.attachments.some(item => item.kind === 'image') && imageSupport(conversation.model) !== 'supported'}
              onRetryAttachment={localAttachments.retryAttachment}
              onAttachmentError={() => onToast('error', t('无法打开文件选择器，请重试'))}
              disabledReason={isConversationHydrationFailed
                ? t('会话加载失败，请先重试')
                : modelCatalogStatus === 'loading'
                  ? t('正在加载模型…')
                  : modelCatalogStatus === 'error'
                    ? t('模型加载失败，请先重试')
                    : modelCatalogStatus === 'empty'
                      ? t('未配置可用模型，请联系管理员或重试')
                      : !isHistoryBootstrapped || historyBootstrapStatus === 'loading'
                        ? t('正在加载历史会话…')
                        : isInitialHistoryUnavailable
                          ? t('历史会话加载失败，请先重试')
                          : undefined}
              onChange={setDraft}
              onSend={send}
              onStop={() => void stop()}
              onExitPlan={exitPlanMode}
              onAddAttachments={localAttachments.addFiles}
              onRemoveAttachment={localAttachments.removeAttachment}
            />
          </>
        ) : (
          <ErrorBoundary
            onError={() => pushToast('error', t('链路区域无法显示'))}
            resetKey={`${conversation.threadId || 'draft'}:chain-trace`}
            fallback={({ reset }) => (
              <div className="chain-trace-state">
                <Button type="button" variant="text" onClick={reset}>{t('重新加载')}</Button>
              </div>
            )}
          >
            <ChainTraceView
              drawerLayout={traceDrawerLayout}
              mobile={navigation.band === 'mobile'}
              onDetailsUnavailable={() => pushToast('info', t('展开窗口后可查看'))}
              onError={(message) => pushToast('error', message)}
              onWarning={notifyChainWarning}
              threadId={conversation.threadId}
              active={workspaceView === 'trace'}
              live={conversation.runStatus === 'streaming' || conversation.runStatus === 'detached'}
              observedAt={conversation.trace?.observedAt}
            />
          </ErrorBoundary>
        )}
        </>}
      </main>
      <ErrorBoundary
        onError={() => pushToast('error', t('任务轨迹无法显示'))}
        resetKey={`${conversation.threadId || 'draft'}:${taskDrawer.open ? 'open' : 'closed'}`}
        fallback={({ reset }) => taskDrawer.open ? (
          <aside ref={taskDrawer.drawerRef} id="todo-trace-drawer" className={`todo-trace-drawer is-open todo-trace-error${taskDetailPageOpen ? ' is-full-page' : ''}`} aria-label={t('任务轨迹无法显示')}>
            <Button type="button" variant="text" onClick={reset}>{t('重新加载')}</Button>
            <Button onClick={() => taskDrawer.close(true)}>{t(taskDetailPageOpen ? '返回对话' : '关闭任务轨迹')}</Button>
          </aside>
        ) : null}
      >
        <TodoTraceDrawer
          groups={conversation.taskTrace.phase === 'ready'
            ? conversation.taskTrace.snapshot.todoGroups
            : []}
          open={taskDrawer.open}
          fullPage={navigation.band === 'mobile'}
          resizeHandle={<DrawerResizeHandle control={taskDrawerLayout} label={t('调整任务抽屉宽度')} controls="todo-trace-drawer" />}
          openEpoch={taskDrawer.openEpoch}
          drawerRef={taskDrawer.drawerRef}
          onClose={() => taskDrawer.close(true)}
          onLocate={locateTodoGroup}
        />
      </ErrorBoundary>
      </div>
      <WorkspaceDialogs
        dialog={dialog}
        pending={dialogPending}
        onConfirm={confirmDialog}
        onCancel={closeDialog}
      />
      <SettingsDialog
        onToast={pushToast}
        onModelsChanged={retryModelCatalog}
        open={settingsOpen}
        user={user}
        themePreference={theme.preference}
        restoreFocusTo={settingsRestoreFocus.current}
        onThemePreferenceChange={theme.selectPreference}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
    </AttachmentReferenceContext.Provider>
    </TextRevealProgressContext.Provider>
  )
}
