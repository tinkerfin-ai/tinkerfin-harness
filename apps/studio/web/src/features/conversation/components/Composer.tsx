import { ArrowUp, Paperclip, Plus, Square } from 'lucide-react'
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import type { EditorState, Transaction } from '@codemirror/state'
import { changeComposerText, composerSkillReferences, deleteComposerAtom, insertComposerSkill } from '../composerDraft'
import { ComposerInput } from './ComposerInput'

import { DraftAttachmentCard } from '../attachments/DraftAttachmentCard'
import { useAttachmentPicker } from '../attachments/useAttachmentPicker'
import { IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import {
  applyAtomicPlanDeletion,
  cancelComposerSuggestion,
  detectComposerSlashToken,
  detectLeadingSlashToken,
  enabledSuggestionIds,
  filterComposerSuggestionGroups,
  hasLeadingSkillReference,
  isAllowedComposerDraft,
  isSubmittableComposerDraft,
  planClaimParts,
  replaceSlashTokenWithPlan,
} from '../composerSuggestions'
import type { DraftAttachment } from '../useAttachments'
import type { ComposerSkill, ComposerSkillsStatus } from '../composerSuggestions'
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
  draft,
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
  onAttachmentError,
  skills = [],
  selectedSkills = [],
  skillsStatus = 'ready',
  onRetrySkills,
}: {
  draft: EditorState
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
  onChange: (transaction: Transaction) => void
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
  onAttachmentError?: () => void
  skills?: readonly ComposerSkill[]
  selectedSkills?: readonly ComposerSkill[]
  skillsStatus?: ComposerSkillsStatus
  onRetrySkills?: () => void
}) {
  const { t } = useI18n()
  const value = draft.doc.toString()
  const references = draft.field(composerSkillReferences)
  const caret = draft.selection.main.head
  const input = useRef<HTMLTextAreaElement>(null)
  const attachmentScroll = useRef<HTMLDivElement>(null)
  const previousAttachmentIds = useRef(new Set(attachments.map(attachment => attachment.id)))
  const attachmentPicker = useAttachmentPicker(onAttachmentError)
  const previousAttachments = useRef(attachments)
  const menuId = `composer-suggestions-${useId()}`
  const [menuRequested, setMenuRequested] = useState(false)
  const [activeSuggestionId, setActiveSuggestionId] = useState<string>()
  const takeoverWasActive = useRef(Boolean(takeover))
  const focusAfterTakeover = useRef(false)
  const isDisabled = isHydrating || Boolean(disabledReason)
  const slashHit = useMemo(
    () => isDisabled ? null : detectComposerSlashToken(value, caret, references),
    [caret, isDisabled, value, references],
  )
  const suggestionGroups = useMemo(
    () => filterComposerSuggestionGroups(menuRequested ? '' : slashHit?.query ?? '', skills, skillsStatus).filter(group => menuRequested || !slashHit || slashHit.start === value.search(/\S/) || group.id === 'skill').map(group => ({
      ...group,
      items: group.items.map(item => item.id === 'compact' ? {
        ...item, disabled: !onCompact || isRunning || Boolean(compactDisabledReason),
        description: compactDisabledReason ?? item.description,
      } : group.id === 'skill' && !item.id.startsWith('skills-') ? {
        ...item, disabled: selectedSkills.some(skill => skill.id === item.id) || selectedSkills.length >= 8,
      } : item),
    })),
    [menuRequested, onCompact, isRunning, compactDisabledReason, skills, skillsStatus, selectedSkills, slashHit, value],
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
  const planClaim = hasLeadingSkillReference(value, references) ? null : planClaimParts(value)
  const isCompactCommand = !hasLeadingSkillReference(value, references) && /^\/compact(?:\s|$)/.test(value.trim())
  const hasUnavailableSkills = selectedSkills.some(skill => skill.unavailable)
  const skillSelectionPending = selectedSkills.length > 0 && skillsStatus !== 'ready'
  const skillSelectionMessage = skillSelectionPending
    ? skillsStatus === 'loading' ? t('正在加载技能') : t('技能列表暂不可用')
    : hasUnavailableSkills ? t('所选技能已停用或卸载，请移除后再发送') : undefined
  const canSubmitDraft = (isCompactCommand || (!skillSelectionPending && !hasUnavailableSkills)) && (Boolean(value.trim()) || attachments.length > 0) && (!value.trim() || isSubmittableComposerDraft(value, references)) && (isCompactCommand ? !compactDisabledReason : attachments.every(item => item.state === 'ready'))
  const cancelSuggestionMenu = useCallback(() => {
    if (menuRequested) {
      setMenuRequested(false)
      return
    }
    const cancellation = cancelComposerSuggestion(value, slashHit ?? undefined)
    onChange(changeComposerText(draft, cancellation.value, cancellation.caret))
  }, [menuRequested, onChange, slashHit, value, draft])


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
    if (id === 'skill-skills-retry') { onRetrySkills?.(); return }
    if (id.startsWith('skill-')) {
      const skill = skills.find(item => `skill-${item.id}` === id)
      if (!skill || selectedSkills.length >= 8 || selectedSkills.some(item => item.id === skill.id)) return
      onChange(insertComposerSkill(draft, skill, menuRequested ? null : slashHit, t('用 {skill} 技能帮我', { skill: `/${skill.name}` })))
      setMenuRequested(false)
      input.current?.focus()
      return
    }
    if (id === 'command-compact') {
      if (isRunning || compactDisabledReason || !onCompact?.()) return
      setMenuRequested(false)
      if (slashHit) {
        const edit = cancelComposerSuggestion(value)
        onChange(changeComposerText(draft, edit.value, edit.caret))
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
    onChange(changeComposerText(draft, replacement.value, replacement.caret))
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
      onChange(changeComposerText(draft, edit.value, edit.caret))
      return
    }
    if (event.key === 'Backspace' || event.key === 'Delete') {
      const atomicEdit = deleteComposerAtom(
        draft,
        event.currentTarget.selectionStart,
        event.currentTarget.selectionEnd,
        event.key === 'Backspace' ? 'backward' : 'forward',
      )
      if (atomicEdit) {
        event.preventDefault()
        onChange(atomicEdit)
        return
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
      event.preventDefault()
      if (canSubmitDraft && !isRunning && !isDisabled) onSend()
    }
  }

  const stopControl = (
    <IconButton
      className="send-button stop"
      label={stopPending ? t('正在停止任务') : canStop ? t('停止任务') : stopDisabledReason ?? t('正在创建会话')}
      icon={<Square size={13} fill="currentColor" />}
      loading={stopPending}
      disabled={!canStop || stopPending}
      onClick={onStop}
    />
  )

  return (
    <footer className={`composer-dock${hero ? ' is-hero' : ''}${takeover ? ' is-taken-over' : ''}`}>
      {(!hero || scrollToBottomControl || taskTraceControl || (takeover && isRunning)) && (
        <div className="composer-auxiliary-controls">
          {scrollToBottomControl && (
            <div className="composer-scroll-to-bottom-control">{scrollToBottomControl}</div>
          )}
          {(taskTraceControl || (takeover && isRunning)) && (
            <div className="composer-task-trace-control" aria-hidden={backgroundInert || undefined} inert={backgroundInert || undefined}>
              {taskTraceControl}
              {takeover && isRunning && stopControl}
            </div>
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
        {skillSelectionMessage && <p className="composer-skill-error" role="status">{skillSelectionMessage}</p>}
        <ComposerInput ref={input} draft={draft} disabled={isDisabled} busy={isHydrating}
          menuId={menuOpen ? menuId : undefined}
          activeId={menuOpen && resolvedActiveId ? `${menuId}-${resolvedActiveId}` : undefined}
          unavailableIds={selectedSkills.filter(skill => skill.unavailable).map(skill => skill.id)}
          onChange={onChange} onKeyDown={handleKeyDown} onBlur={() => setMenuRequested(false)}
          onAddAttachments={onAddAttachments}
          placeholder={isHydrating ? t('正在加载会话…') : disabledReason ?? t('给 TinkerFin 发消息')}
          acceptDraft={nextDraft => {
            const nextValue = nextDraft.doc.toString()
            const nextCaret = nextDraft.selection.main.head
            const nextReferences = nextDraft.field(composerSkillReferences)
            setMenuRequested(false)
            const nextSlashHit = detectComposerSlashToken(nextValue, nextCaret, nextReferences)
            return isAllowedComposerDraft(nextValue, skills, nextReferences) || Boolean(nextSlashHit
              && enabledSuggestionIds(filterComposerSuggestionGroups(nextSlashHit.query, skills, skillsStatus)).length > 0)
          }} />
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
              accept=".png,.jpg,.jpeg,.webp,.gif,.pdf,.docx,.xlsx,.pptx,.md,.markdown,.zip"
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
              !takeover && stopControl
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
