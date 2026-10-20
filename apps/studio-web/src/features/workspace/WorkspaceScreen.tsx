import { getServerAddress } from '../../api/shared/config'
import { MemoriesPage } from '../memories/MemoriesPage'
import { MoveConversationDialog } from '../projects/MoveConversationDialog'
import { useProjectConversationActions } from '../projects/useProjectConversationActions'
import { ProjectsWorkspace, type ProjectWorkspaceScope } from '../projects/ProjectsWorkspace'
import { ProjectSwitcher } from '../projects/ProjectSwitcher'
import type { CSSProperties } from 'react'
import { FolderClosed, Workflow } from 'lucide-react'
import { WorkspaceFilesDrawer } from '../workspaceFiles/WorkspaceFilesDrawer'
import { useWorkspaceFiles } from '../workspaceFiles/useWorkspaceFiles'
import { restoreFocus } from '../../components/ui/focus'
import { useDrawerLayout } from '../../components/ui/useDrawerLayout'
import { DrawerResizeHandle } from '../../components/ui/DrawerResizeHandle'
import { isConversationRunning } from '../../lib/workspace'
import { AccessModePicker } from "../../components/AccessModePicker"
import { AutomationPage } from '../automation/AutomationPage'
import { SkillsPage } from '../skills/SkillsPage'
import { readRunSkillSelection } from '../skills/api'
import { AttachmentReferenceContext } from '../conversation/attachments/context'
import { TextRevealProgressContext } from '../conversation/components/textRevealProgress'
import { messageText, messageAttachments } from '../conversation/attachments/content'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import type { AgentMode, ChatRequestPayload } from '../../api/conversation/types'
import type { TodoGroup } from '../../api/conversation/taskTrace'
import { conversationErrorMessage } from '../../api/conversation/errors'
import type { AuthUser } from '../../api/auth/types'
import {
  Button,
  Drawer,
  IconButton,
  ErrorBoundary,
  Dialog,
  FeedbackState,
  useThemePreference,
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
import { useComposerDraft } from '../conversation/useComposerDraft'
import { useContextCompaction } from '../conversation/compaction/useContextCompaction'
import { useAttachments } from '../conversation/useAttachments'
import { useComposerSkills } from '../conversation/useComposerSkills'
import { useInteractionConfirmation } from '../conversation/useInteractionConfirmation'
import { markInteractionRequestRejected } from '../conversation/interactionConfirmation'
import { ComposerModelPicker } from './components/ComposerModelPicker'
import { Sidebar } from './components/Sidebar'
import { ConversationSearchDialog } from './components/ConversationSearchDialog'
import {
  ConversationViewport,
} from './components/ConversationViewport'
import { EmptyConversationBrand } from './components/EmptyConversation'
import { WorkspaceDialogs } from './components/WorkspaceDialogs'
import { WorkspaceHeader } from './components/WorkspaceHeader'
import { ScrollToBottomButton } from './components/ScrollToBottomButton'
import { SettingsDialog } from '../settings/SettingsDialog'
import { DevicePicker } from '../devices/DevicePicker'
import { useDevices } from '../devices/useDevices'
import { fetchRunLocalTarget, type LocalTarget } from '../devices/api'
import { useExecutionLocation } from '../devices/useExecutionLocation'
import { useWorkspaceNavigation } from './useWorkspaceNavigation'
import { useModelCatalog } from './useModelCatalog'
import { useWorkspaceHistory } from './useWorkspaceHistory'
import { useWorkspaceState } from './useWorkspaceState'
import { ConversationNavigator } from '../conversation/navigation/ConversationNavigator'
import { conversationTurns } from '../conversation/navigation/turns'
import { useConversationWidth } from '../conversation/width/useConversationWidth'
import { ConversationWidthHandles } from '../conversation/width/ConversationWidthHandles'
import { useConversationScroll } from './useConversationScroll'
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
  upsertConversation,
  selectCurrentConversation,
  updateConversation,
} from '../../lib/workspace'
import { clearSettingsLocation, readDeviceSettingsFromLocation, readPageFromLocation, readProjectFromLocation, readThreadFromLocation, writeWorkspaceToLocation } from '../../lib/threadRoute'
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

type WorkspaceScreenProps = { user: AuthUser; onLogout: () => void; onToast: ToastHandler }

export function WorkspaceScreen(props: WorkspaceScreenProps) {
  const [workspaceFilesOpen, setWorkspaceFilesOpen] = useState(false)
  const [settings, setSettings] = useState<{ section: 'user' | 'devices'; trigger: HTMLElement | null } | null>(() => readDeviceSettingsFromLocation() ? { section: 'devices', trigger: null } : null)
  const devices = useDevices()
  const theme = useThemePreference()
  const modelCatalog = useModelCatalog()
  useEffect(() => {
    const navigate = () => setSettings(readDeviceSettingsFromLocation() ? { section: 'devices', trigger: null } : null)
    window.addEventListener('popstate', navigate)
    return () => window.removeEventListener('popstate', navigate)
  }, [])
  const openSettings = useCallback((trigger: HTMLElement | null) => setSettings({ section: 'user', trigger }), [])
  return <>
    <div style={{ display: 'contents' }} inert={settings !== null || undefined} aria-hidden={settings !== null || undefined}>
      <ProjectsWorkspace key={props.user.user_id} user={props.user} onLogout={props.onLogout} onToast={props.onToast}>{scope => <ProjectWorkspaceScreen
        key={scope.project.id} {...props} scope={scope} devices={devices} modelCatalog={modelCatalog}
        settingsOpen={settings !== null} onOpenSettings={openSettings}
        workspaceFilesOpen={workspaceFilesOpen} onWorkspaceFilesOpenChange={setWorkspaceFilesOpen} />}</ProjectsWorkspace>
    </div>
    <SettingsDialog devices={devices} onToast={props.onToast} onModelsChanged={modelCatalog.retry}
      open={settings !== null} initialSection={settings?.section ?? 'user'} user={props.user}
      themePreference={theme.preference} onThemePreferenceChange={theme.selectPreference}
      restoreFocusTo={settings?.trigger} onClose={() => { setSettings(null); clearSettingsLocation() }} />
  </>
}

function ProjectWorkspaceScreen({
  scope,
  user,
  onLogout,
  onToast,
  workspaceFilesOpen,
  onWorkspaceFilesOpenChange,
  devices,
  modelCatalog,
  settingsOpen,
  onOpenSettings,
}: {
  user: AuthUser
  onLogout: () => void
  onToast: ToastHandler
  scope: ProjectWorkspaceScope
  workspaceFilesOpen: boolean
  onWorkspaceFilesOpenChange: (open: boolean) => void
  devices: ReturnType<typeof useDevices>
  modelCatalog: ReturnType<typeof useModelCatalog>
  settingsOpen: boolean
  onOpenSettings: (trigger: HTMLElement | null) => void
}) {
  const { t } = useI18n()
  const projectId = scope.project.id
  const [archivedHistory, setArchivedHistory] = useState(false)
  const [searchScope, setSearchScope] = useState<'project' | 'all'>('project')
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchReturnTo, setSearchReturnTo] = useState<HTMLElement | null>(null)
  const {
    workspace,
    setWorkspace,
    retainConversationDetails,
    setComposerPreference,
    acknowledgeComposerPreferences,
  } = useWorkspaceState()
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
  const draftKey = workspace.currentThreadId ? `thread:${workspace.currentThreadId}` : `${projectId}:${pendingConversations.selectedRunId ? `pending:${pendingConversations.selectedRunId}` : ''}`
  const {
    target: localTarget, state: targetState, locked: targetLocked,
    isAvailable: localTargetAvailable, retry: retryTargetRead, select: selectLocalTarget,
    recordSubmission: recordLocalSubmission, registerThread: registerLocalThread,
  } = useExecutionLocation({
    projectId, userId: user.user_id, threadId: workspace.currentThreadId,
    pendingRunId: pendingConversations.selectedRunId, draftKey, devices,
    onPreferenceError: () => onToast('error', t('无法保存执行位置偏好')),
  })
  const targetKnown = targetState === 'ready'
  const localTargetDisabledReason = !targetKnown ? t(targetState === 'error' ? '执行位置加载失败，请重试' : '正在加载执行位置') : !localTarget ? undefined : devices.status !== 'ready'
    ? t('请先重新加载设备列表') : !localTargetAvailable(localTarget) ? t('请在客户端重新连接') : undefined
  const composerDraft = useComposerDraft('', { key: draftKey, store: scope.drafts })
  const draft = composerDraft.text
  const setDraft = composerDraft.setText
  const draftRevision = composerDraft.revision
  const submissionLocks = useRef(new Map<string, string>())
  const [isModelPickerOpen, setModelPickerOpen] = useState(false)
  const [pendingResume, setPendingResume] = useState<PendingResume | null>(null)
  const [activePage, setActivePage] = useState(readPageFromLocation)
  const [automationEntryKey, setAutomationEntryKey] = useState(0)
  const composerSkills = useComposerSkills(projectId, activePage === 'conversation', composerDraft.references)
  const retrySkillRequest = useRef<AbortController | null>(null)
  const [automationModalOpen, setAutomationModalOpen] = useState(false)
  const [skillsModalOpen, setSkillsModalOpen] = useState(false)
  const [memoriesModalOpen, setMemoriesModalOpen] = useState(false)
  const [workspaceView, setWorkspaceView] = useState<'conversation' | 'trace'>('conversation')
  const filesDrawerOpen = workspaceFilesOpen && activePage === 'conversation' && workspaceView === 'conversation' && targetKnown && !localTarget
  const workspaceFiles = useWorkspaceFiles(projectId, filesDrawerOpen)
  const workspaceFilesLauncherRef = useRef<HTMLButtonElement>(null)
  const closeWorkspaceFiles = useCallback(() => {
    onWorkspaceFilesOpenChange(false)
    window.requestAnimationFrame(() => restoreFocus(workspaceFilesLauncherRef.current, { preventScroll: true }))
  }, [onWorkspaceFilesOpenChange])
  useEffect(() => {
    if (activePage !== 'conversation' || workspaceView !== 'conversation') onWorkspaceFilesOpenChange(false)
  }, [activePage, workspaceView, onWorkspaceFilesOpenChange])
  const navigation = useWorkspaceNavigation()
  const {
    status: modelCatalogStatus,
    modelIds,
    defaultModelId,
    models,
    retry: retryModelCatalog,
  } = modelCatalog
  const localAttachments = useAttachments(projectId, (message) => onToast(
    'error',
    isTranslationKey(message) ? t(message) : message,
  ), { key: draftKey, store: scope.attachments })
  const projectActions = useProjectConversationActions(workspace, setWorkspace, onToast,
    target => localAttachments.validateProjectMove(`thread:${target.threadId}`))
  const appShell = useRef<HTMLDivElement>(null)
  const latestWorkspace = useRef(workspace)
  const startedResumeRunIds = useRef(new Set<string>())
  const [initialActiveSessions] = useState(() => readActiveRunSessions().filter(session => session.projectId === projectId))
  const autoRecoveredRunIds = useRef(new Set<string>())
  const notifiedConversationEvents = useRef(new Set<string>())
  const notifiedChainWarnings = useRef(new Set<string>())
  latestWorkspace.current = workspace
  const pushToast = onToast
  const notifyConversation = useCallback((notice: NonNullable<Conversation['notice']>) => {
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
    if (modelCatalogStatus === 'ready' && !workspace.currentThreadId && !pendingConversations.selectedRunId && defaultModelId) {
      setDraftModel((current) => current || defaultModelId)
    }
  }, [defaultModelId, modelCatalogStatus, workspace.currentThreadId, pendingConversations.selectedRunId])

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
    projectId,
    workspace,
    setWorkspace,
    retainConversationDetails,
    acknowledgeComposerPreferences,
    setDraftConversation,
    onNotice: notifyConversation,
  })
  const {
    cancelInitialSelection,
    historyConversations,
    historyDayRanges,
    historyQuery,
    setHistoryQuery,
    isHistorySearching,
    searchConversations, searchCursor, searchLoadError, isSearchLoadingMore, loadMoreSearchHistory, retryHistorySearch,
    historyCursor,
    isHistoryLoadingMore,
    historyLoadError,
    isHistoryBootstrapped,
    historyBootstrapStatus,
    hydrationState,
    historyRefresh,
    recheckConversationHistory,
    taskTraceLoadFailed,
    loadMoreHistory,
    retryHistoryLoad,
    retryHistoryBootstrap,
    hydrateConversation,
    retryTaskTrace,
    loadOlderTrace,
  } = useWorkspaceHistory({
    projectId, archived: archivedHistory, searchScope, searchOpen,
    preferDraft: Boolean(scope.drafts.get(`${projectId}:`)?.doc.length || scope.attachments.get(`${projectId}:`)?.length),
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
  const selectedThreadId = selectedConversation?.threadId
  const selectedArchived = selectedConversation?.archived
  useEffect(() => {
    if (selectedThreadId && selectedArchived !== undefined) setArchivedHistory(selectedArchived)
  }, [selectedThreadId, selectedArchived])
  const conversation = useMemo(() => {
    if (workspace.currentThreadId) {
      return selectedConversation
        ?? buildEmptyConversation({ projectId,
          threadId: workspace.currentThreadId,
          now: new Date().toISOString(),
          model: draftModel,
        })
    }
    return draftConversation ?? buildEmptyConversation({ projectId, now: new Date().toISOString(), model: draftModel, accessMode: draftAccessMode })
  }, [projectId, draftConversation, draftModel, draftAccessMode, selectedConversation, workspace.currentThreadId])

  const isSelectedModelAvailable = modelCatalogStatus === 'ready' && modelIds.includes(conversation.model)
  // 等待原技能期间改变会话、提交选项或模型目录后，必须由用户重新发起重试
  useLayoutEffect(() => () => retrySkillRequest.current?.abort(), [
    workspace.currentThreadId, conversation.model, conversation.mode, conversation.accessMode, modelCatalogStatus, modelIds, localTarget,
  ])
  const modelSubmissionDisabledReason = modelCatalogStatus === 'ready' && !isSelectedModelAvailable
    ? t('请选择可用模型') : undefined
  const isFailedInitialSubmission = !conversation.threadId && conversation.runStatus === 'error'
  const pendingInitial = pendingConversations.items.find(item => item.runId === pendingConversations.selectedRunId)
  const canRecoverDisplayed = !isFailedInitialSubmission || Boolean(
    pendingInitial && modelCatalogStatus === 'ready' && modelIds.includes(pendingInitial.conversation.model),
  )

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
    const notification = conversation.notice
    if (notification && !notification.id?.endsWith(':terminal')) notifyConversation(notification)
  }, [conversation, notifyConversation])

  useEffect(() => {
    if (activePage !== 'conversation') {
      const pageTitles = { skills: t('技能库'), automation: t('定时任务'), memories: t('记忆管理') }
      document.title = `TinkerFin - ${pageTitles[activePage]}`
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
    conversation.approval
    || conversation.planInteraction
    || conversation.pendingInteractionKind,
  )
  const isRunning = isConversationRunning(conversation)
  const canStopConversation = Boolean(conversation.threadId && (
    isActiveThread(conversation.threadId)
    || (conversation.trace?.status.execution === 'running'
      && conversation.trace.headRunId === conversation.activeRunId)
  ))
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
  const filesDrawerLayout = useDrawerLayout(640, resizeConversationContent, 560)
  const { hostRef: taskHostRef } = taskDrawerLayout
  const { hostRef: filesHostRef } = filesDrawerLayout
  const drawerHostRef = useCallback((node: HTMLDivElement | null) => {
    taskHostRef(node)
    filesHostRef(node)
  }, [taskHostRef, filesHostRef])
  const traceDrawerLayout = useDrawerLayout(520)
  const taskDrawer = useTodoTraceDrawer({
    threadId: conversation.threadId,
    taskTrace: conversation.taskTrace,
    blocked: taskTraceBlocked || filesDrawerOpen,
    available: navigation.band === 'mobile' || taskDrawerLayout.available,
  })
  const taskDetailPageOpen = navigation.band === 'mobile' && taskDrawer.open
  const filesDetailPageOpen = filesDrawerOpen && !filesDrawerLayout.available
  const detailPageOpen = taskDetailPageOpen || filesDetailPageOpen
  const closeTaskDrawer = taskDrawer.close
  useWorkspaceLayoutAnimation({
    shellRef: appShell,
    layoutKey: `${navigation.mode}:${filesDrawerOpen && !filesDetailPageOpen ? 'files' : taskDrawer.open && !taskDetailPageOpen ? 'tasks' : 'closed'}`,
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
  const interactionConfirmation = useInteractionConfirmation(conversation, (threadId, updater) => {
    setWorkspace(state => updateConversation(state, threadId, updater))
  })

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
      if (readProjectFromLocation() && readProjectFromLocation() !== projectId) return
      cancelInitialSelection()
      selectPendingConversation(null)
      submissionLocks.current.delete('')
      releaseDraft()
      const threadId = readThreadFromLocation()
      setActivePage(readPageFromLocation())
      setWorkspaceView('conversation')
      setWorkspace(state => {
        if (state.currentThreadId === threadId) return state
        // 路由可以指向尚未分页加载的会话，正文仍由现有历史加载流程授权读取
        const target = !threadId || state.conversations.some(item => item.threadId === threadId)
          ? state : upsertConversation(state, { ...buildEmptyConversation({
            projectId, threadId, model: defaultModelId, now: new Date().toISOString(),
          }), isHydrated: false })
        return selectCurrentConversation(target, threadId)
      })
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [projectId, defaultModelId, cancelInitialSelection, releaseDraft, selectPendingConversation, setWorkspace])

  useEffect(() => {
    if (!workspace.currentThreadId || !selectedConversation?.isHydrated) return
    const active = readActiveRunSession(selectedConversation.threadId, projectId)
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
    projectId,
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
    if (!isSelectedModelAvailable || (!resubmission && localTargetDisabledReason)) return
    const submittedTarget = resubmission ? resubmission.meta?.localTarget : localTarget
    if (submittedTarget && !localTargetAvailable(submittedTarget)) { pushToast('error', t('请在客户端重新连接')); return }
    const trimmed = content.trim()
    if (!resubmission && composerSkills.selected.length > 0
      && (composerSkills.status !== 'ready' || composerSkills.selected.some(skill => skill.unavailable))) return
    const readyAttachments = resubmission ? resubmission.attachments ?? [] : localAttachments.attachments.flatMap(item => item.attachment ? [item.attachment] : [])
    const submissionKey = workspace.currentThreadId
    if (submissionLocks.current.has(submissionKey) || (submissionKey && isActiveThread(submissionKey))) return
    if ((!trimmed && !readyAttachments.length) || isRunning || !conversation.model || (!resubmission && localAttachments.attachments.some(item => item.state !== 'ready'))) return
    if (!conversation.threadId && conversation.runStatus === 'detached') return
    if (resubmission && (conversation.runStatus === 'detached' || conversation.pendingInteractionKind || conversation.approval || conversation.planInteraction)) return
    const submittedIds = localAttachments.attachments.map(item => item.id)
    const submittedThreadId = workspace.currentThreadId
    const submittedDraft = composerDraft.state
    const submittedSkills = composerSkills.selected
    const selectedSkills = resubmission ? resubmission.meta?.selectedSkills ?? [] : submittedSkills.map(({ id, name }) => ({ id, name }))
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
        composerDraft.restore(submittedDraft)
      }
    }
    messageWindow.restoreTail()
    const effectiveMode = modeOverride ?? conversation.mode

    const now = new Date().toISOString()
    if (!workspace.currentThreadId) {
      const nextConversation = buildEmptyConversation({ projectId,
        now,
        model: draftConversation?.model ?? draftModel,
        mode: effectiveMode,
        accessMode: conversation.accessMode,
      })
      const payload = buildInitialPayload(nextConversation, content, readyAttachments, selectedSkills.map(skill => skill.id), submittedTarget)
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
          meta: { runId: payload.runId, selectedSkills, localTarget: submittedTarget },
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
      const pendingKey = `${projectId}:pending:${payload.runId}`
      recordLocalSubmission(pendingKey, submittedTarget)
      composerDraft.moveTo(pendingKey)
      localAttachments.moveTo(pendingKey)
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
        onRegistered: (threadId, selected) => {
          const key = `thread:${threadId}`
          registerLocalThread(pendingKey, threadId, submittedTarget)
          if (selected) { composerDraft.moveTo(key); localAttachments.moveTo(key) }
          else {
            const text = scope.drafts.get(pendingKey)
            if (text) scope.drafts.set(key, text)
            scope.drafts.delete(pendingKey)
            const files = scope.attachments.get(pendingKey)
            if (files) scope.attachments.set(key, files)
            scope.attachments.delete(pendingKey)
          }
          pendingConversations.remove(payload.runId)
        },
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
    const payload = buildInitialPayload(sendingConversation, content, readyAttachments, selectedSkills.map(skill => skill.id), submittedTarget)
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
          meta: { runId: payload.runId, selectedSkills, localTarget: submittedTarget },
        }],
      }))
    })
    if (!resubmission) setDraft('')
    void streamRun(currentConversation.threadId, payload, 'start', { target: 'workspace', onAccepted, onRequestRejected }).finally(() => releaseSubmission(payload.runId))
  }, [localTarget, localTargetDisabledReason, localTargetAvailable, recordLocalSubmission, registerLocalThread, pushToast, scope.attachments, scope.drafts, projectId, isActiveThread, isSelectedModelAvailable, t, conversation, localAttachments, composerSkills, composerDraft, draftRevision, draftConversation?.model, draftModel, pendingConversations, discardDraft, setHistoryQuery, hydrateConversation, isRunning, messageWindow, scrollConversationToBottomImmediately, setDraft, setComposerPreference, setWorkspace, streamRun, workspace.conversations, workspace.currentThreadId])

  const latestBeginSend = useRef(beginSend)
  useLayoutEffect(() => { latestBeginSend.current = beginSend }, [beginSend])

  const retryRun = useCallback(async (message: Message) => {
    const current = latestWorkspace.current.conversations.find(item => item.threadId === workspace.currentThreadId)
    const original = current?.messages.find(item => item.id === message.id && item.role === 'user')
    if (!current?.isHydrated || !original || original.meta?.contentOmitted) return
    if (!current.runFailures?.some(item => item.runId === original.meta?.runId && item.retryable)) return
    const runId = original.meta?.runId
    if (!runId) return
    retrySkillRequest.current?.abort()
    const controller = new AbortController()
    retrySkillRequest.current = controller
    try {
      const [selectedSkills, originalTarget] = await Promise.all([
        readRunSkillSelection(current.threadId, runId, controller.signal),
        fetchRunLocalTarget(current.threadId, runId, controller.signal),
      ])
      if (controller.signal.aborted || latestWorkspace.current.currentThreadId !== current.threadId) return
      latestBeginSend.current(original.content, undefined, { ...original, meta: { ...original.meta, selectedSkills, localTarget: originalTarget ?? undefined } })
    } catch {
      if (!controller.signal.aborted) pushToast('error', t('无法读取原运行配置，请重试'))
    } finally {
      if (retrySkillRequest.current === controller) retrySkillRequest.current = null
    }
  }, [workspace.currentThreadId, pushToast, t])

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
      { target: 'workspace', onRequestRejected: () => {
        setWorkspace(state => updateConversation(state, pendingResume.threadId, current => markInteractionRequestRejected(
          current, pendingResume.threadId, pendingResume.payload.runId, pendingResume.expectedInterruptIds,
        )))
      } },
    )
  }, [pendingResume, setWorkspace, streamRun, workspace.conversations])

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
          : { ...prepared, activeRunId: payload.runId, historySynchronized: false,
              approval: prepared.approval ? { ...prepared.approval, submissionRunId: payload.runId } : undefined }
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
            submissionRunId: payload.runId,
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
        ? { ...item.planInteraction, submitted: true, submissionRunId: payload.runId, error: undefined }
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
      item.approval && !item.approval.submitted
        ? { ...item, approval: updater(item.approval), historySynchronized: false }
        : item
    )))
  }, [setWorkspace])

  const changePlanInteraction = useCallback((
    threadId: string,
    updater: (interaction: PlanInteraction) => PlanInteraction,
  ) => {
    setWorkspace((state) => updateConversation(state, threadId, (item) => (
      item.planInteraction && !item.planInteraction.submitted
        ? { ...item, planInteraction: updater(item.planInteraction), historySynchronized: false }
        : item
    )))
  }, [setWorkspace])

  const onConversationBoundary = () => {
    cancelInitialSelection()
    messageWindow.captureReadingPosition()
    setActivePage('conversation')
    pendingConversations.select(null)
    submissionLocks.current.delete('')
    releaseDraft()
    draftRevision.current += 1
    setWorkspaceView('conversation')
  }

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
    setDraftConversation,
    setDraftModel,
    setDraftAccessMode,
    defaultModelId,
    followDetachedConversation,
    abandonPlanInteraction,
    cancelRun,
    isActiveThread,
    onToast: pushToast,
    onConversationBoundary,
  })

  const changeExecutionLocation = (value: LocalTarget | undefined) => {
    if (selectLocalTarget(value)) localAttachments.revalidateExecution(`${projectId}:`)
  }

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
      pushToast('info', cancelled === undefined
        ? t('连接已中断，尚无法确认任务状态，请恢复连接')
        : cancelled ? t('任务已停止') : t('任务已经结束'))
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

  const recoverDisplayedConversation = () => {
    if (!canRecoverDisplayed) return
    void recoverConversation(conversation.threadId, pendingConversations.selectedRunId ?? undefined)
  }

  const compaction = useContextCompaction({
    conversation, setWorkspace, streamRun, isActiveThread,
    onNotice: message => pushToast('info', message),
  })

  const executeCompaction = () => {
    if (!isSelectedModelAvailable || !compaction.execute()) return false
    messageWindow.restoreTail()
    scrollConversationToBottomImmediately()
    return true
  }

  const send = () => {
    const submission = parseComposerSubmission(draft, composerDraft.references)
    if (!submission) return
    if (submission.kind === 'compact') {
      if (executeCompaction()) setDraft('')
      return
    }
    if (submission.kind === 'compact-arguments-unsupported') {
      pushToast('info', t('请单独使用 /compact，不附加其他内容'))
      return
    }
    if (submission.kind === 'plan-off-unsupported') {
      pushToast('info', t('请点击输入框中的 Plan 按钮关闭'))
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

  const closeConversationSearch = () => {
    setSearchOpen(false)
    setHistoryQuery('')
    setSearchScope('project')
  }

  // Portal 对话框打开时整块工作区退出辅助技术与键盘路径，只保留最上层操作
  const portalModalActive = searchOpen || Boolean(projectActions.moving) || memoriesModalOpen || scope.modalOpen || settingsOpen || dialog != null || directoryOpen || automationModalOpen || skillsModalOpen
  const taskTraceLauncher = taskTraceBlocked ? undefined : (
    <TodoTraceLauncher
      ref={taskDrawer.launcherRef}
      taskTrace={conversation.taskTrace}
      open={taskDrawer.open}
      loadFailed={taskTraceLoadFailed}
      onToggle={() => {
        setWorkspaceView('conversation')
        onWorkspaceFilesOpenChange(false)
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
        onOpenConversation={() => { setActivePage('conversation'); navigation.closeOverlay(false) }}
        onOpenMemories={() => { messageWindow.captureReadingPosition(); taskDrawer.close(false); changeDirectoryOpen(false); navigation.closeOverlay(false); setActivePage('memories') }}
        memoriesActive={activePage === 'memories'}
        archivedHistory={archivedHistory} onArchivedHistoryChange={value => {
          onConversationBoundary()
          setDraftConversation(null)
          setArchivedHistory(value)
        }}
        onArchive={projectActions.archive} onMove={projectActions.move}
        projectSelector={<ProjectSwitcher scope={scope} />}
        onChooseProject={() => { navigation.requestExpanded(); requestAnimationFrame(() => appShell.current?.querySelector<HTMLButtonElement>('.project-switcher-trigger')?.focus()) }}
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
        searchOpen={searchOpen}
        searchTriggerRef={setSearchReturnTo}
        onOpenSearch={() => {
          navigation.closeOverlay(false)
          setSearchOpen(true)
        }}
        mode={navigation.mode}
        overlayOpen={navigation.overlayOpen}
        wideInteractive={navigation.wideInteractive}
        railInteractive={navigation.railInteractive}
        onToggleMode={navigation.toggleDesktopMode}
        onRequestExpanded={navigation.requestExpanded}
        onCloseOverlay={navigation.closeOverlay}
        automationActive={activePage === 'automation'}
        skillsActive={activePage === 'skills'}
        onOpenSkills={() => {
          messageWindow.captureReadingPosition()
          taskDrawer.close(false)
          changeDirectoryOpen(false)
          navigation.closeOverlay(false)
          setActivePage('skills')
        }}
        onOpenAutomation={() => {
          messageWindow.captureReadingPosition()
          taskDrawer.close(false)
          changeDirectoryOpen(false)
          navigation.closeOverlay(false)
          setAutomationEntryKey(value => value + 1)
          setActivePage('automation')
        }}
        onNew={() => {
          setActivePage('conversation')
          setWorkspaceView('conversation')
          newConversation()
          setDraftModel('')
          retryModelCatalog()
        }}
        onSelect={(threadId) => {
          const target = workspace.conversations.find(item => item.threadId === threadId)
          if (target && target.projectId !== projectId) { scope.select(target.projectId, threadId); return }
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
          onOpenSettings(restoreFocusTo ?? null)
        }}
        onLogout={onLogout}
        backgroundInert={portalModalActive}
      />
      <div ref={drawerHostRef} className={`workspace-content${detailPageOpen ? ' has-task-detail-page' : ''}`} style={{ '--layout-drawer-width': `${filesDrawerOpen ? filesDrawerLayout.width : taskDrawerLayout.width}px` } as CSSProperties}>
      <main
        ref={conversationWidth.rootRef}
        data-workspace-layout-target="main"
        id="main-content"
        className="workspace-main"
        aria-hidden={detailPageOpen || (navigation.mode === 'overlay' && navigation.overlayOpen) || undefined}
        inert={detailPageOpen || (navigation.mode === 'overlay' && navigation.overlayOpen) || undefined}
      >
        {activePage === 'memories' ? (
          <ErrorBoundary fallback={({ reset }) => <div className="memories-page"><p>{t('记忆区域无法显示')}</p><Button type="button" onClick={reset}>{t('重新加载')}</Button></div>}>
            <MemoriesPage project={scope.project} navigationTriggerRef={navigation.overlayTriggerRef} onOpenNavigation={navigation.openOverlay} onModalChange={setMemoriesModalOpen} onToast={pushToast} />
          </ErrorBoundary>
        ) : activePage === 'skills' ? (
          <ErrorBoundary onError={() => pushToast('error', t('技能区域无法显示'))}
            fallback={({ reset }) => <div className="skills-empty"><p>{t('技能区域无法显示')}</p><Button type="button" onClick={reset}>{t('重新加载')}</Button></div>}>
            <SkillsPage project={scope.project} navigationTriggerRef={navigation.overlayTriggerRef} onOpenNavigation={navigation.openOverlay} onModalChange={setSkillsModalOpen} onToast={pushToast} />
          </ErrorBoundary>
        ) : activePage === 'automation' ? (
          <ErrorBoundary onError={() => pushToast('error', t('定时任务区域无法显示'))}
            fallback={({ reset }) => <div className="automation-empty"><p>{t('定时任务区域无法显示')}</p><Button type="button" onClick={reset}>{t('重新加载')}</Button></div>}>
            <AutomationPage key={automationEntryKey} projectId={projectId} executions={scope.automation} onOpenConversation={(run, attachment, signal) => scope.openRunConversation(run, {
              signal,
              prepareDraft: attachment ? target => localAttachments.addReference(attachment, { key: `thread:${target.threadId}`, store: scope.attachments }) : undefined,
            })} navigationTriggerRef={navigation.overlayTriggerRef} onOpenNavigation={navigation.openOverlay}
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
          actions={<div className="workspace-header-tools" role="group" aria-label={t('会话操作')}>
            {taskTraceLauncher}
            <IconButton type="button" size="xs" label={t('链路')} tooltip={t('链路')} icon={<Workflow size={17} />}
              selected={workspaceView === 'trace'} aria-controls="chain-trace-panel" disabled={!conversation.threadId}
              onClick={() => selectWorkspaceView(workspaceView === 'trace' ? 'conversation' : 'trace')} />
            <IconButton type="button" ref={workspaceFilesLauncherRef} size="xs" className="workspace-header-action"
              label={t('工作区')} tooltip={t('工作区')} aria-expanded={localTarget ? undefined : filesDrawerOpen}
              disabled={!targetKnown} aria-controls={localTarget ? undefined : "workspace-files-drawer"} icon={<FolderClosed size={17} />}
              onClick={() => {
                if (!targetKnown) return
                if (localTarget) {
                  const query = new URLSearchParams({ server: getServerAddress(), namespace: `ns_${user.user_id}`,
                    device: localTarget.device_id, project: projectId })
                  window.location.href = `tinkerfin://workspace?${query}`
                  return
                }
                resizeConversationContent(() => {
                  setWorkspaceView('conversation')
                  taskDrawer.close(false)
                  onWorkspaceFilesOpenChange(!filesDrawerOpen)
                })
              }} />
          </div>}
          navigation={<DevicePicker targetState={targetState} onRetry={retryTargetRead} devices={devices} projectId={projectId} value={localTarget} onChange={changeExecutionLocation}
            disabled={isConversationHydrating || isConversationHydrationFailed || !targetKnown}
            locked={targetLocked} />}
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
              onRetryHydration={() => void hydrateConversation(conversation.threadId, { refresh: true })}
              onError={(message) => pushToast('error', message)}
              onRecoverConversation={canRecoverDisplayed ? recoverDisplayedConversation : undefined}
              onRetryRun={retryRun}
              retryDisabled={isRunning || conversation.runStatus === 'detached' || !conversation.isHydrated || !isSelectedModelAvailable || Boolean(conversation.pendingInteractionKind || conversation.approval || conversation.planInteraction)}
              onLoadEarlierMessages={(trigger) => void messageWindow.loadEarlierMessages(trigger)}
            />
            <Composer
              draft={composerDraft.state}
              skills={composerSkills.skills}
              selectedSkills={composerSkills.selected}
              skillsStatus={composerSkills.status}
              onRetrySkills={composerSkills.retry}
              isRunning={isRunning}
              canStop={canStopConversation && !compaction.saving}
              stopDisabledReason={compaction.saving ? t('正在保存压缩结果') : !canStopConversation && conversation.threadId ? t('恢复连接后可停止任务') : undefined}
              stopPending={Boolean(conversation.activeRunId && cancelPendingRunId === conversation.activeRunId)}
              isHydrating={isConversationHydrating}
              submission={conversation.approval?.submitted || conversation.planInteraction?.submitted ? interactionConfirmation : undefined}
              hero={showConversationHero ? <EmptyConversationBrand /> : undefined}
              takeover={conversation.approval
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
              onChooseModel={() => setModelPickerOpen(true)}
              onCompact={executeCompaction}
              compactDisabledReason={modelSubmissionDisabledReason ?? compaction.disabledReason}
              submissionDisabledReason={modelSubmissionDisabledReason ?? localTargetDisabledReason}
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
              onRetryAttachment={localAttachments.retryAttachment}
              onAttachmentError={() => onToast('error', t('无法打开文件选择器，请重试'))}
              disabledReason={conversation.archived ? t('此会话已归档，恢复后可继续') : isConversationHydrationFailed
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
              onChange={composerDraft.apply}
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
              liveRunId={conversation.activeRunId}
              observation={conversation.trace}
              historyRefresh={historyRefresh}
              onRecheckHistory={recheckConversationHistory}
            />
          </ErrorBoundary>
        )}
        </>}
      </main>
      <ErrorBoundary
        onError={() => pushToast('error', t('任务轨迹无法显示'))}
        resetKey={`${conversation.threadId || 'draft'}:${taskDrawer.open ? 'open' : 'closed'}`}
        fallback={({ reset }) => taskDrawer.open ? (
          <Drawer drawerRef={taskDrawer.drawerRef} id="todo-trace-drawer" open fullPage={taskDetailPageOpen}
            className="todo-trace-drawer todo-trace-error" title={t('任务轨迹无法显示')}
            closeLabel={t('关闭任务轨迹')} backLabel={t('返回对话')} onClose={() => taskDrawer.close(true)}>
            <Button type="button" variant="text" onClick={reset}>{t('重新加载')}</Button>
          </Drawer>
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
      <ErrorBoundary resetKey={`${projectId}:${filesDrawerOpen}`} fallback={({ reset }) => filesDrawerOpen ? (
        <Drawer id="workspace-files-drawer" open title={t('工作区')} description={scope.project.name} fullPage={filesDetailPageOpen}
          closeLabel={t('关闭工作区')} backLabel={t('返回对话')} onClose={closeWorkspaceFiles}>
          <FeedbackState kind="error" title={t('工作区无法显示')} onRetry={reset} />
        </Drawer>
      ) : null}>
        <WorkspaceFilesDrawer state={workspaceFiles} open={filesDrawerOpen}
          fullPage={filesDetailPageOpen} onClose={closeWorkspaceFiles} onToast={pushToast}
          resizeHandle={<DrawerResizeHandle control={filesDrawerLayout} label={t('调整工作区宽度')} controls="workspace-files-drawer" />} />
      </ErrorBoundary>
      </div>
      {searchOpen && <ErrorBoundary
        fallback={({ reset }) => <Dialog open title={t('搜索会话')} className="modal-dialog--action conversation-search-dialog"
          restoreFocusTo={navigation.mode === 'overlay' ? navigation.overlayTriggerRef.current : searchReturnTo} onClose={closeConversationSearch}>
          <div className="conversation-search-body"><FeedbackState kind="error" appearance="retry" title={t('搜索会话失败')} onRetry={reset} /></div>
        </Dialog>}>
        <ConversationSearchDialog query={historyQuery} scope={searchScope} results={searchConversations}
          projectNames={Object.fromEntries(scope.projects.map(project => [project.id, project.name]))}
          loading={isHistorySearching} loadingMore={isSearchLoadingMore} error={searchLoadError}
          hasMore={searchCursor != null} restoreFocusTo={navigation.mode === 'overlay' ? navigation.overlayTriggerRef.current : searchReturnTo}
          onQueryChange={setHistoryQuery} onScopeChange={setSearchScope}
          onLoadMore={() => loadMoreSearchHistory(true)} onRetry={retryHistorySearch} onClose={closeConversationSearch}
          onSelect={target => {
            closeConversationSearch()
            if (target.projectId !== projectId) { scope.select(target.projectId, target.threadId); return }
            setActivePage('conversation')
            selectConversation(target.threadId)
          }} />
      </ErrorBoundary>}
      {projectActions.moving && <MoveConversationDialog conversation={projectActions.moving.conversation} projects={scope.projects} trigger={projectActions.moving.trigger} pending={projectActions.pending} error={projectActions.error} onConfirm={projectActions.confirmMove} onClose={projectActions.close} />}
      <WorkspaceDialogs
        dialog={dialog}
        pending={dialogPending}
        onConfirm={confirmDialog}
        onCancel={closeDialog}
      />
    </div>
    </AttachmentReferenceContext.Provider>
    </TextRevealProgressContext.Provider>
  )
}
