import { ArrowUp, Paperclip, Plus, Square } from 'lucide-react'
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

import { DraftAttachmentCard } from '../attachments/DraftAttachmentCard'
import { useAttachmentPicker } from '../attachments/useAttachmentPicker'
import { IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import {
  applyAtomicPlanDeletion,
  cancelComposerSuggestion,
  detectLeadingSlashToken,
  enabledSuggestionIds,
  filterComposerSuggestionGroups,
  isAllowedComposerDraft,
  isSubmittableComposerDraft,
  planClaimParts,
  replaceSlashTokenWithPlan,
} from '../composerSuggestions'
import type { DraftAttachment } from '../useAttachments'
import { ComposerPlanChip } from './ComposerPlanChip'
import { ComposerSuggestionMenu } from './ComposerSuggestionMenu'

const APPLE_PLATFORM = /Mac|iPhone|iPad|iPod/

const deleteToLogicalLineStart = (
  value: string,
  selectionStart: number,
  selectionEnd: number,
) => {
  const lineStart = value.lastIndexOf('\n', Math.max(-1, selectionStart - 1)) + 1
  return applyAtomicPlanDeletion(value, lineStart, selectionEnd, 'backward') ?? {
    value: `${value.slice(0, lineStart)}${value.slice(selectionEnd)}`,
    caret: lineStart,
  }
}

export function Composer({
  value,
  isRunning,
  canStop = true,
  stopDisabledReason,
  stopPending = false,
  isHydrating = false,
  disabledReason,
  hero,
  takeover,
  scrollToBottomControl,
  taskTraceControl,
  backgroundInert = false,
  modelControl,
  accessControl,
  planActive,
  planLocked = false,
  attachments,
  onChange,
  onSend,
  onStop,
  onExitPlan,
  onChooseModel,
  onCompact,
  compactDisabledReason,
  onAddAttachments,
  onRemoveAttachment,
  onRetryAttachment,
  attachmentBlocked = false,
  onAttachmentError,
}: {
  value: string
  isRunning: boolean
  canStop?: boolean
  stopDisabledReason?: string
  stopPending?: boolean
  isHydrating?: boolean
  disabledReason?: string
  hero?: ReactNode
  takeover?: ReactNode
  scrollToBottomControl?: ReactNode
  taskTraceControl?: ReactNode
  backgroundInert?: boolean
  modelControl: ReactNode
  accessControl?: ReactNode
  planActive: boolean
  planLocked?: boolean
  attachments: readonly DraftAttachment[]
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  onExitPlan: () => void
  /** 打开宿主持有的模型选择器，沿用当前会话的选择结果与焦点操作 */
  onChooseModel: () => void
  onCompact?: () => boolean
  compactDisabledReason?: string
  onAddAttachments: (files: readonly File[]) => void
  onRemoveAttachment: (id: string) => void
  onRetryAttachment?: (id: string) => void
  attachmentBlocked?: boolean
  onAttachmentError?: () => void
}) {
  const { t } = useI18n()
  const input = useRef<HTMLTextAreaElement>(null)
  const inputScroll = useRef<HTMLDivElement>(null)
  const attachmentScroll = useRef<HTMLDivElement>(null)
  const previousAttachmentIds = useRef(new Set(attachments.map(attachment => attachment.id)))
  const attachmentPicker = useAttachmentPicker(onAttachmentError)
  const previousAttachments = useRef(attachments)
  const pendingCaret = useRef<number | null>(null)
  const acceptedCaret = useRef(value.length)
  const menuId = `composer-suggestions-${useId()}`
  const [caret, setCaret] = useState(value.length)
  const [menuRequested, setMenuRequested] = useState(false)
  const [activeSuggestionId, setActiveSuggestionId] = useState<string>()
  const takeoverWasActive = useRef(Boolean(takeover))
  const focusAfterTakeover = useRef(false)
  const isDisabled = isHydrating || Boolean(disabledReason)
  const slashHit = useMemo(
    () => isDisabled ? null : detectLeadingSlashToken(value, caret),
    [caret, isDisabled, value],
  )
  const suggestionGroups = useMemo(
    () => filterComposerSuggestionGroups(menuRequested ? '' : slashHit?.query ?? '').map(group => ({
      ...group,
      items: group.items.map(item => item.id === 'compact' ? {
        ...item, disabled: !onCompact || isRunning || Boolean(compactDisabledReason),
        description: compactDisabledReason ?? item.description,
      } : item),
    })),
    [menuRequested, slashHit?.query, onCompact, isRunning, compactDisabledReason],
  )
  const enabledIds = useMemo(
    () => enabledSuggestionIds(suggestionGroups),
    [suggestionGroups],
  )
  const menuOpen = Boolean(
    !isDisabled
    && (menuRequested || slashHit)
    && suggestionGroups.some((group) => group.items.length > 0),
  )
  const resolvedActiveId = enabledIds.includes(activeSuggestionId ?? '')
    ? activeSuggestionId
    : enabledIds[0]
  const planClaim = planClaimParts(value)
  const isCompactCommand = /^\/compact(?:\s|$)/.test(value.trim())
  const canSubmitDraft = (Boolean(value.trim()) || attachments.length > 0) && (!value.trim() || isSubmittableComposerDraft(value)) && (isCompactCommand ? !compactDisabledReason : !attachmentBlocked && attachments.every(item => item.state === 'ready'))
  const cancelSuggestionMenu = useCallback(() => {
    if (menuRequested) {
      setMenuRequested(false)
      return
    }
    const cancellation = cancelComposerSuggestion(value, slashHit ?? undefined)
    pendingCaret.current = cancellation.caret
    onChange(cancellation.value)
  }, [menuRequested, onChange, slashHit, value])

  useLayoutEffect(() => {
    if (pendingCaret.current == null) return
    const nextCaret = pendingCaret.current
    pendingCaret.current = null
    input.current?.setSelectionRange(nextCaret, nextCaret)
    acceptedCaret.current = nextCaret
    setCaret(nextCaret)
  }, [menuRequested, value])

  useEffect(() => {
    const wasActive = takeoverWasActive.current
    takeoverWasActive.current = Boolean(takeover)
    if (wasActive && !takeover) focusAfterTakeover.current = true
    if (takeover) focusAfterTakeover.current = false
    if (!focusAfterTakeover.current || isDisabled) return
    const frame = window.requestAnimationFrame(() => {
      input.current?.focus()
      focusAfterTakeover.current = false
    })
    return () => window.cancelAnimationFrame(frame)
  }, [isDisabled, takeover])

  useEffect(() => {
    const addedReference = attachments.some(item => item.reference && !previousAttachments.current.some(previous => previous.id === item.id))
    previousAttachments.current = attachments
    if (!addedReference || isDisabled || backgroundInert || takeover) return
    const frame = window.requestAnimationFrame(() => input.current?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [attachments, backgroundInert, isDisabled, takeover])

  useEffect(() => {
    const previousIds = previousAttachmentIds.current
    const hasAddedAttachment = attachments.some(attachment => !previousIds.has(attachment.id))
    previousAttachmentIds.current = new Set(attachments.map(attachment => attachment.id))
    if (!hasAddedAttachment) return
    const frame = window.requestAnimationFrame(() => {
      const element = attachmentScroll.current
      const lastAttachment = element?.lastElementChild
      if (lastAttachment instanceof HTMLElement && typeof lastAttachment.scrollIntoView === 'function')
        lastAttachment.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'end' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [attachments])

  const pickSuggestion = (id: string) => {
    if (id === 'command-compact') {
      if (isRunning || compactDisabledReason || !onCompact?.()) return
      setMenuRequested(false)
      if (slashHit) {
        const edit = cancelComposerSuggestion(value)
        pendingCaret.current = edit.caret
        onChange(edit.value)
      }
      input.current?.focus()
      return
    }
    if (id === 'command-model') {
      cancelSuggestionMenu()
      onChooseModel()
      return
    }
    if (id !== 'command-plan') return
    const hit = slashHit ?? detectLeadingSlashToken(value, value.length) ?? {
      start: planClaim?.leading.length ?? 0,
      end: planClaim ? planClaim.leading.length + planClaim.token.length : 0,
      query: '',
    }
    const replacement = replaceSlashTokenWithPlan(value, hit)
    setMenuRequested(false)
    pendingCaret.current = replacement.caret
    onChange(replacement.value)
    input.current?.focus()
  }

  const moveSuggestion = (direction: 1 | -1) => {
    if (enabledIds.length === 0) return
    const currentIndex = Math.max(0, enabledIds.indexOf(resolvedActiveId ?? ''))
    setActiveSuggestionId(
      enabledIds[(currentIndex + direction + enabledIds.length) % enabledIds.length],
    )
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
    if (menuOpen) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        moveSuggestion(event.key === 'ArrowDown' ? 1 : -1)
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        cancelSuggestionMenu()
        return
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && resolvedActiveId) {
        event.preventDefault()
        pickSuggestion(resolvedActiveId)
        return
      }
    }
    if (
      event.key.toLowerCase() === 'u'
      && event.ctrlKey
      && !event.metaKey
      && !event.altKey
      && !event.shiftKey
      && APPLE_PLATFORM.test(window.navigator.platform || window.navigator.userAgent)
      && !event.nativeEvent.isComposing
      && event.nativeEvent.keyCode !== 229
    ) {
      event.preventDefault()
      const edit = deleteToLogicalLineStart(
        value,
        event.currentTarget.selectionStart,
        event.currentTarget.selectionEnd,
      )
      pendingCaret.current = edit.caret
      onChange(edit.value)
      return
    }
    if (event.key === 'Backspace' || event.key === 'Delete') {
      const atomicEdit = applyAtomicPlanDeletion(
        value,
        event.currentTarget.selectionStart,
        event.currentTarget.selectionEnd,
        event.key === 'Backspace' ? 'backward' : 'forward',
      )
      if (atomicEdit) {
        event.preventDefault()
        pendingCaret.current = atomicEdit.caret
        onChange(atomicEdit.value)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
      event.preventDefault()
      if (canSubmitDraft && !isRunning && !isDisabled) onSend()
    }
  }

  return (
    <footer className={`composer-dock${hero ? ' is-hero' : ''}${takeover ? ' is-taken-over' : ''}`}>
      {(!hero || scrollToBottomControl || taskTraceControl) && (
        <div className="composer-auxiliary-controls">
          {scrollToBottomControl && (
            <div className="composer-scroll-to-bottom-control">{scrollToBottomControl}</div>
          )}
          {taskTraceControl && (
            <div className="composer-task-trace-control">{taskTraceControl}</div>
          )}
        </div>
      )}
      <div
        className={`composer-default${takeover ? ' is-taken-over' : ''}`}
        aria-hidden={Boolean(takeover) || backgroundInert || undefined}
        inert={Boolean(takeover) || backgroundInert || undefined}
      >
        {hero && <div className="composer-hero">{hero}</div>}
        <div className="composer" onPointerDown={(event) => {
        if (event.target instanceof Element && event.target.closest('button')) return
        input.current?.focus()
      }} onWheel={(event) => event.stopPropagation()}>
        {menuOpen && (
          <ComposerSuggestionMenu
            id={menuId}
            groups={suggestionGroups}
            activeId={resolvedActiveId}
            onPick={pickSuggestion}
            onDismiss={cancelSuggestionMenu}
          />
        )}
        {attachments.length > 0 && (
          <div ref={attachmentScroll} className="composer-attachments" aria-label={t('待发送附件')}>

            {attachments.map((attachment) => (
              <DraftAttachmentCard key={attachment.id} attachment={attachment} onRemove={onRemoveAttachment} onRetry={onRetryAttachment} />
            ))}
          </div>
        )}
        <div ref={inputScroll} className="composer-input-scroll">
          <div className="composer-input-grow">
            <div className={`composer-input-backdrop${isDisabled ? ' is-disabled' : ''}`} aria-hidden="true">
              {planClaim ? (
                <>
                  {planClaim.leading}
                  <mark>{planClaim.token}</mark>
                  {planClaim.content || <span>{t('描述你的任务以生成计划')}</span>}
                </>
              ) : value}
            </div>
            <textarea
              ref={input}
              className="composer-input"
              aria-label={t('消息输入')}
              aria-busy={isHydrating}
              aria-controls={menuOpen ? menuId : undefined}
              aria-activedescendant={menuOpen && resolvedActiveId ? `${menuId}-${resolvedActiveId}` : undefined}
              aria-autocomplete="list"
              disabled={isDisabled}
              value={value}
              onPaste={(event) => { const files = [...event.clipboardData.files]; if (files.length) { event.preventDefault(); onAddAttachments(files) } }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => { event.preventDefault(); onAddAttachments([...event.dataTransfer.files]) }}
              onChange={(event) => {
                setMenuRequested(false)
                const nextValue = event.target.value
                const nextCaret = event.target.selectionStart
                const nextSlashHit = detectLeadingSlashToken(nextValue, nextCaret)
                const keepsEnabledSuggestion = Boolean(
                  nextSlashHit
                  && enabledSuggestionIds(
                    filterComposerSuggestionGroups(nextSlashHit.query),
                  ).length > 0,
                )
                if (!isAllowedComposerDraft(nextValue) && !keepsEnabledSuggestion) {
                  const previousCaret = acceptedCaret.current
                  window.requestAnimationFrame(() => {
                    input.current?.setSelectionRange(previousCaret, previousCaret)
                    setCaret(previousCaret)
                  })
                  return
                }
                acceptedCaret.current = nextCaret
                pendingCaret.current = nextCaret
                setCaret(nextCaret)
                onChange(nextValue)
              }}
              onSelect={(event) => {
                const nextCaret = event.currentTarget.selectionStart
                acceptedCaret.current = nextCaret
                setCaret(nextCaret)
              }}
              onKeyDown={handleKeyDown}
              onBlur={() => setMenuRequested(false)}
              rows={1}
              placeholder={isHydrating ? t('正在加载会话…') : disabledReason ?? t('给 TinkerFin 发消息')}
            />
            <div className="composer-input-mirror" aria-hidden="true">{`${value}\n`}</div>
          </div>
        </div>
        <div className="composer-toolbar-container">
        <div className="composer-toolbar">
          <div className="composer-toolbar-leading">
            {/* 加号内留白较多，收紧视框并补偿线宽，使可见大小与附件、权限图标一致 */}
            <IconButton
              type="button"
              size="sm"
              className="composer-toolbar-button"
              label={t('打开命令和技能')}
              icon={<Plus size={16} viewBox="3.6 3.6 16.8 16.8" strokeWidth={1.4} />}
              aria-haspopup="listbox"
              aria-expanded={menuOpen}
              aria-controls={menuOpen ? menuId : undefined}
              disabled={isDisabled}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                if (menuOpen) cancelSuggestionMenu()
                else {
                  setActiveSuggestionId(undefined)
                  setMenuRequested(true)
                }
                input.current?.focus()
              }}
            />
            <input
              ref={attachmentPicker.inputRef}
              type="file"
              hidden
              multiple
              accept=".png,.jpg,.jpeg,.webp,.gif,.pdf,.docx,.xlsx,.pptx,.md,.markdown"
              onChange={(event) => {
                onAddAttachments(Array.from(event.target.files ?? []))
                event.target.value = ''
              }}
            />
            <IconButton
              ref={attachmentPicker.buttonRef}
              type="button"
              size="sm"
              className="composer-toolbar-button"
              label={t('添加本地附件')}
              tooltip={t('添加本地附件')}
              icon={<Paperclip size={16} />}
              loading={attachmentPicker.pending}
              onClick={attachmentPicker.open}
            />
            {accessControl}
            {planActive && (
              <ComposerPlanChip locked={planLocked} onExitPlan={() => {
                onExitPlan()
                input.current?.focus({ preventScroll: true })
              }} />
            )}
          </div>
          <div className="composer-toolbar-trailing">
            {modelControl}
            {isRunning ? (
              <IconButton
                className="send-button stop"
                label={stopPending ? t('正在停止任务') : canStop ? t('停止任务') : stopDisabledReason ?? t('正在创建会话')}
                icon={<Square size={13} fill="currentColor" />}
                loading={stopPending}
                disabled={!canStop || stopPending}
                onClick={onStop}
              />
            ) : (
              <IconButton className="send-button" label={t('发送消息')} icon={<ArrowUp size={18} />} disabled={isDisabled || !canSubmitDraft} onClick={onSend} />
            )}
          </div>
        </div>
        </div>
        </div>
      </div>
      {takeover && (
        <div
          className="composer-takeover"
          aria-hidden={backgroundInert || undefined}
          inert={backgroundInert || undefined}
        >
          {takeover}
        </div>
      )}
      <p className="composer-note">{t('TinkerFin 可能会犯错，请核对重要信息')}</p>
    </footer>
  )
}
