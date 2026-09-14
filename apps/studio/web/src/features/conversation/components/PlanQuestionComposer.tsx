import {
  CalendarClock,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Clock3,
  MessageSquareText,
} from 'lucide-react'
import {
  forwardRef,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'

import { Button, DatePicker, IconButton, OverlayScrollbar, TimePicker } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import type { PlanQuestionItem, PlanQuestionState } from '../../../types'
import {
  readPlanQuestionCollapsed,
  writePlanQuestionCollapsed,
} from '../planQuestionCollapse'
import { PlanInteractionCard, PlanInteractionStatusRow } from './PlanInteractionCard'

const multipleSelectionMaximum = (
  question: Extract<PlanQuestionItem, { answerType: 'multiple_choice' }>,
) => question.maxSelections
  ?? question.options.length + Number(question.allowFreeText)

const multipleSelectionCount = (
  question: Extract<PlanQuestionItem, { answerType: 'multiple_choice' }>,
) => question.selectedOptionIds.length + Number(Boolean(question.customAnswer?.trim()))

const splitLocalDateTime = (value: string | undefined) => {
  const match = /^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):([0-5]\d)$/.exec(value ?? '')
  return match
    ? { date: match[1]!, time: `${match[2]}:${match[3]}` }
    : undefined
}

const timeBoundsForDate = (
  date: string,
  minimum: string | undefined,
  maximum: string | undefined,
) => {
  const minimumParts = splitLocalDateTime(minimum)
  const maximumParts = splitLocalDateTime(maximum)
  return {
    minimum: minimumParts?.date === date ? minimumParts.time : undefined,
    maximum: maximumParts?.date === date ? maximumParts.time : undefined,
  }
}

const clampTime = (
  value: string,
  minimum: string | undefined,
  maximum: string | undefined,
) => {
  if (minimum && value < minimum) return minimum
  if (maximum && value > maximum) return maximum
  return value
}

const PlanDateTimeControl = forwardRef<HTMLButtonElement, {
  value: string | undefined
  prompt: string
  minimum?: string
  maximum?: string
  onChange: (value: string) => void
}>(function PlanDateTimeControl({
  value,
  prompt,
  minimum,
  maximum,
  onChange,
}, forwardedRef) {
  const { t } = useI18n()
  const parts = splitLocalDateTime(value)
  const minimumParts = splitLocalDateTime(minimum)
  const maximumParts = splitLocalDateTime(maximum)
  const bounds = parts
    ? timeBoundsForDate(parts.date, minimum, maximum)
    : { minimum: undefined, maximum: undefined }

  return (
    <span className="plan-question-datetime-controls">
      <DatePicker
        ref={forwardedRef}
        controlSize="xs"
        value={parts?.date ?? ''}
        min={minimumParts?.date}
        max={maximumParts?.date}
        label={t('日期回答：{question}', { question: prompt })}
        onChange={(date) => {
          const nextBounds = timeBoundsForDate(date, minimum, maximum)
          const nextTime = clampTime(
            parts?.time ?? nextBounds.minimum ?? '00:00',
            nextBounds.minimum,
            nextBounds.maximum,
          )
          onChange(`${date}T${nextTime}`)
        }}
      />
      <TimePicker
        controlSize="xs"
        value={parts?.time ?? ''}
        label={t('时间回答：{question}', { question: prompt })}
        min={bounds.minimum}
        max={bounds.maximum}
        disabled={!parts}
        onChange={(time) => {
          if (parts) onChange(`${parts.date}T${time}`)
        }}
      />
    </span>
  )
})

const minuteTimeIsValid = (
  question: Extract<PlanQuestionItem, { answerType: 'time' }>,
) => Boolean(
  question.time
  && /^([01]\d|2[0-3]):[0-5]\d$/.test(question.time)
  && (question.minimum == null || question.time >= question.minimum)
  && (question.maximum == null || question.time <= question.maximum),
)

const minuteDateTimeIsValid = (
  question: Extract<PlanQuestionItem, { answerType: 'datetime' }>,
) => Boolean(
  question.dateTime
  && /^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$/.test(question.dateTime)
  && (question.minimum == null || question.dateTime >= question.minimum)
  && (question.maximum == null || question.dateTime <= question.maximum),
)

const questionAnswerState = (question: PlanQuestionItem): 'answered' | 'missing' | 'invalid' => {
  if (question.skipped) return question.required ? 'invalid' : 'answered'
  if (question.answerType === 'single_choice') {
    return question.selectedOptionId || question.customAnswer?.trim() ? 'answered' : 'missing'
  }
  if (question.answerType === 'multiple_choice') {
    const count = multipleSelectionCount(question)
    if (count === 0) return 'missing'
    return count >= question.minSelections && count <= multipleSelectionMaximum(question)
      ? 'answered'
      : 'invalid'
  }
  if (question.answerType === 'text') return question.answer?.trim() ? 'answered' : 'missing'
  if (question.answerType === 'date') return question.date ? 'answered' : 'missing'
  if (question.answerType === 'datetime') {
    if (!question.dateTime) return 'missing'
    return minuteDateTimeIsValid(question) ? 'answered' : 'invalid'
  }
  if (!question.time) return 'missing'
  return minuteTimeIsValid(question) ? 'answered' : 'invalid'
}

const questionAnswered = (question: PlanQuestionItem) => {
  return questionAnswerState(question) === 'answered'
}

const questionHasDraftAnswer = (question: PlanQuestionItem) => {
  if (question.answerType === 'single_choice') {
    return Boolean(question.selectedOptionId || question.customAnswer?.trim())
  }
  if (question.answerType === 'multiple_choice') {
    return question.selectedOptionIds.length > 0 || Boolean(question.customAnswer?.trim())
  }
  if (question.answerType === 'text') return Boolean(question.answer?.trim())
  if (question.answerType === 'date') return Boolean(question.date)
  if (question.answerType === 'datetime') return Boolean(question.dateTime)
  return Boolean(question.time)
}

const preferredOptionIndex = (question: PlanQuestionItem | undefined) => {
  if (question?.answerType === 'single_choice' && question.selectedOptionId) {
    const selectedIndex = question.options.findIndex((option) => (
      option.id === question.selectedOptionId
    ))
    if (selectedIndex >= 0) return selectedIndex
  }
  return 0
}

const clearQuestionAnswer = (question: PlanQuestionItem): PlanQuestionItem => {
  if (question.answerType === 'single_choice') {
    return {
      ...question,
      selectedOptionId: undefined,
      customAnswer: '',
      skipped: true,
    }
  }
  if (question.answerType === 'multiple_choice') {
    return {
      ...question,
      selectedOptionIds: [],
      customAnswer: '',
      skipped: true,
    }
  }
  if (question.answerType === 'text') {
    return { ...question, answer: '', skipped: true }
  }
  if (question.answerType === 'date') return { ...question, date: '', skipped: true }
  if (question.answerType === 'datetime') {
    return { ...question, dateTime: '', skipped: true }
  }
  return { ...question, time: '', skipped: true }
}

const resizeTextAnswer = (textarea: HTMLTextAreaElement | null) => {
  if (!textarea) return
  textarea.style.height = 'auto'
  const maxHeight = Number.parseFloat(window.getComputedStyle(textarea).maxHeight)
  const contentHeight = textarea.scrollHeight
  if (contentHeight <= 0) return
  const nextHeight = Number.isFinite(maxHeight)
    ? Math.min(contentHeight, maxHeight)
    : contentHeight
  textarea.style.height = `${nextHeight}px`
  textarea.style.overflowY = contentHeight > nextHeight ? 'auto' : 'hidden'
}

export function PlanQuestionStatusRow({ interaction }: { interaction: PlanQuestionState }) {
  const { t } = useI18n()
  return (
    <PlanInteractionStatusRow
      kind="question"
      icon={<CircleHelp size={14} aria-hidden="true" />}
      label={t('提问')}
      pendingStatus={t('等待回答')}
      submittedStatus={t('正在继续规划')}
      submitted={interaction.submitted}
    />
  )
}

export function PlanQuestionComposer({
  threadId,
  interaction,
  onChange,
  onSubmit,
}: {
  threadId: string
  interaction: PlanQuestionState
  onChange: (updater: (current: PlanQuestionState) => PlanQuestionState) => void
  onSubmit: () => void
}) {
  const { t } = useI18n()
  const [minimized, setMinimized] = useState(() => readPlanQuestionCollapsed(threadId))
  const [focusedAnswerIndex, setFocusedAnswerIndex] = useState(0)
  const optionRefs = useRef<Array<HTMLElement | null>>([])
  const textAnswerRef = useRef<HTMLTextAreaElement | null>(null)
  const dateAnswerRef = useRef<HTMLButtonElement | null>(null)
  const timeAnswerRef = useRef<HTMLButtonElement | null>(null)
  const dateTimeAnswerRef = useRef<HTMLButtonElement | null>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const activeIndex = Math.min(
    interaction.activeQuestionIndex,
    interaction.questions.length - 1,
  )
  const question = interaction.questions[activeIndex]
  const questionRef = useRef(question)
  questionRef.current = question
  const questionEntryKey = `${interaction.interruptId}\u0000${activeIndex}\u0000${question?.id ?? ''}`
  const textValue = question?.answerType === 'text'
    ? question.answer
    : question?.answerType === 'single_choice' || question?.answerType === 'multiple_choice'
      ? question.customAnswer
      : undefined

  useEffect(() => {
    setMinimized(readPlanQuestionCollapsed(threadId))
  }, [threadId])

  useEffect(() => {
    const enteredQuestion = questionRef.current
    const optionIndex = preferredOptionIndex(enteredQuestion)
    setFocusedAnswerIndex(optionIndex)
    if (minimized || !enteredQuestion || !questionHasDraftAnswer(enteredQuestion)) return
    const frame = window.requestAnimationFrame(() => {
      const target = enteredQuestion.answerType === 'single_choice'
        ? enteredQuestion.selectedOptionId
          ? optionRefs.current[optionIndex]
          : textAnswerRef.current
        : enteredQuestion.answerType === 'multiple_choice'
          ? optionRefs.current[0]
          : enteredQuestion.answerType === 'text'
            ? textAnswerRef.current
            : enteredQuestion.answerType === 'date'
              ? dateAnswerRef.current
              : enteredQuestion.answerType === 'datetime'
                ? dateTimeAnswerRef.current
              : timeAnswerRef.current
      target?.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [questionEntryKey, minimized])

  useEffect(() => {
    if (minimized) return
    resizeTextAnswer(textAnswerRef.current)
  }, [activeIndex, minimized, textValue])

  const progress = useMemo(() => interaction.questions.map((item, index) => ({
    index,
    state: index === activeIndex
      ? 'current'
      : item.skipped
        ? 'skipped'
        : questionAnswered(item)
          ? 'answered'
          : 'pending',
  })), [activeIndex, interaction.questions])

  if (!question) return null

  const hasNextQuestion = activeIndex < interaction.questions.length - 1
  const canAdvance = hasNextQuestion && (!question.required || questionAnswered(question))

  const setActiveQuestion = (index: number) => {
    onChange((current) => ({
      ...current,
      activeQuestionIndex: Math.max(0, Math.min(index, current.questions.length - 1)),
      error: undefined,
    }))
  }

  const advanceQuestion = () => {
    if (!canAdvance) return
    setActiveQuestion(activeIndex + 1)
  }

  const updateQuestion = (
    update: (current: PlanQuestionItem) => PlanQuestionItem,
    nextIndex?: number,
  ) => {
    onChange((current) => ({
      ...current,
      activeQuestionIndex: nextIndex ?? current.activeQuestionIndex,
      error: undefined,
      questions: current.questions.map((item, index) => (
        index === activeIndex ? update(item) : item
      )),
    }))
  }

  const selectSingleOption = (optionId: string, advance = true) => {
    const nextIndex = advance && activeIndex < interaction.questions.length - 1
      ? activeIndex + 1
      : activeIndex
    updateQuestion((current) => current.answerType === 'single_choice'
      ? {
          ...current,
          selectedOptionId: optionId,
          customAnswer: '',
          skipped: false,
        }
      : current, nextIndex)
  }

  const toggleMultipleOption = (optionId: string) => {
    updateQuestion((current) => {
      if (current.answerType !== 'multiple_choice') return current
      const selected = new Set(current.selectedOptionIds)
      if (selected.has(optionId)) selected.delete(optionId)
      else {
        if (multipleSelectionCount(current) >= multipleSelectionMaximum(current)) {
          return current
        }
        selected.add(optionId)
      }
      return {
        ...current,
        selectedOptionIds: current.options
          .map((option) => option.id)
          .filter((id) => selected.has(id)),
        skipped: false,
      }
    })
  }

  const selectSingleByIndex = (index: number) => {
    if (question.answerType !== 'single_choice') return
    const next = (index + question.options.length) % question.options.length
    const option = question.options[next]
    if (!option) return
    setFocusedAnswerIndex(next)
    selectSingleOption(option.id, false)
    optionRefs.current[next]?.focus()
  }

  const handleSingleKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    optionId: string,
  ) => {
    if (question.answerType !== 'single_choice') return
    if (
      event.key === 'ArrowDown'
      || event.key === 'ArrowRight'
      || event.key === 'ArrowUp'
      || event.key === 'ArrowLeft'
    ) {
      event.preventDefault()
      const direction = event.key === 'ArrowDown' || event.key === 'ArrowRight' ? 1 : -1
      selectSingleByIndex(focusedAnswerIndex + direction)
      return
    }
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      selectSingleByIndex(event.key === 'Home' ? 0 : question.options.length - 1)
      return
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      selectSingleOption(optionId)
    }
  }

  const continueFromText = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'ArrowUp' && event.currentTarget.value === '') {
      const lastOption = optionRefs.current.at(-1)
      if (lastOption) {
        event.preventDefault()
        lastOption.focus()
      }
      return
    }
    if (
      event.key !== 'Enter'
      || event.shiftKey
      || event.nativeEvent.isComposing
      || event.nativeEvent.keyCode === 229
    ) return
    event.preventDefault()
    if (!event.currentTarget.value.trim()) return
    if (activeIndex < interaction.questions.length - 1) setActiveQuestion(activeIndex + 1)
  }

  const updateTextValue = (value: string) => {
    updateQuestion((current) => {
      if (current.answerType === 'single_choice') {
        return {
          ...current,
          selectedOptionId: undefined,
          customAnswer: value,
          skipped: false,
        }
      }
      if (current.answerType === 'multiple_choice') {
        if (
          value.trim()
          && !current.customAnswer?.trim()
          && multipleSelectionCount(current) >= multipleSelectionMaximum(current)
        ) return current
        return { ...current, customAnswer: value, skipped: false }
      }
      if (current.answerType === 'text') {
        return { ...current, answer: value, skipped: false }
      }
      return current
    })
  }

  const skipQuestion = () => {
    if (question.required) return
    const nextIndex = activeIndex < interaction.questions.length - 1
      ? activeIndex + 1
      : activeIndex
    updateQuestion(clearQuestionAnswer, nextIndex)
  }

  const submit = () => {
    const focusCurrentAnswer = () => {
      const optionIndex = preferredOptionIndex(question)
      if (question.answerType === 'single_choice') setFocusedAnswerIndex(optionIndex)
      window.requestAnimationFrame(() => (
        optionRefs.current[optionIndex]
        ?? textAnswerRef.current
        ?? dateAnswerRef.current
        ?? dateTimeAnswerRef.current
        ?? timeAnswerRef.current
      )?.focus())
    }
    const firstInvalid = interaction.questions.findIndex((item) => (
      questionAnswerState(item) === 'invalid'
    ))
    if (firstInvalid >= 0) {
      const invalidQuestion = interaction.questions[firstInvalid]
      onChange((current) => ({
        ...current,
        activeQuestionIndex: firstInvalid,
        error: invalidQuestion?.answerType === 'time'
          ? t('Plan 时间答案超出允许范围')
          : invalidQuestion?.answerType === 'datetime'
            ? t('Plan 日期时间答案超出允许范围')
          : t('Plan 澄清答案超出允许的选择数量'),
      }))
      if (firstInvalid === activeIndex) focusCurrentAnswer()
      return
    }
    const firstMissing = interaction.questions.findIndex((item) => (
      item.required && questionAnswerState(item) === 'missing'
    ))
    if (firstMissing >= 0) {
      onChange((current) => ({
        ...current,
        activeQuestionIndex: firstMissing,
        error: t('请回答所有必填的 Plan 澄清问题'),
      }))
      if (firstMissing === activeIndex) focusCurrentAnswer()
      return
    }
    onSubmit()
  }

  const toggleMinimized = () => {
    setMinimized((current) => {
      const next = !current
      writePlanQuestionCollapsed(threadId, next)
      return next
    })
  }

  const customAnswerAllowed = (
    question.answerType === 'single_choice' || question.answerType === 'multiple_choice'
  ) && question.allowFreeText
  const customAnswer = question.answerType === 'text'
    ? question.answer ?? ''
    : question.answerType === 'single_choice' || question.answerType === 'multiple_choice'
      ? question.customAnswer ?? ''
      : ''
  const customAnswerDisabled = question.answerType === 'multiple_choice'
    && !question.customAnswer?.trim()
    && question.selectedOptionIds.length >= multipleSelectionMaximum(question)

  return (
    <PlanInteractionCard
      kind="question"
      ariaLabel={t('Plan 澄清问题')}
      minimized={minimized}
      icon={<CircleHelp size={16} aria-hidden="true" />}
      title={interaction.title}
      titleMeta={minimized ? <>· {t('第 {current} / {total} 题', {
        current: activeIndex + 1,
        total: interaction.questions.length,
      })}</> : undefined}
      description={minimized ? question.prompt : interaction.description}
      toggleSurfaceLabel={minimized
        ? t('点击标题区域展开问题卡片')
        : t('点击标题区域收起问题卡片')}
      toggleLabel={minimized ? t('展开问题卡片') : t('收起问题卡片')}
      onToggle={toggleMinimized}
      bodyRef={bodyRef}
      minimizedContent={(
        <nav className="plan-question-progress" aria-label={t('问题进度')}>
          {progress.map((item) => (
            <button
              key={interaction.questions[item.index]?.id}
              type="button"
              className={`plan-question-progress-step is-${item.state}`}
              aria-label={t('查看第 {current} 题：{question}', {
                current: item.index + 1,
                question: interaction.questions[item.index]?.prompt ?? '',
              })}
              aria-current={item.state === 'current' ? 'step' : undefined}
              onClick={() => setActiveQuestion(item.index)}
            >
              <span aria-hidden="true" />
            </button>
          ))}
        </nav>
      )}
    >
      <>
        <div
          ref={bodyRef}
          className="plan-question-composer-body ui-scrollbar"
          role="region"
          aria-label={question.prompt}
        >
          <h3>
            <span>{question.prompt}</span>
            {!question.required && <small>{t('可选')}</small>}
          </h3>
          <div className="plan-question-options">
            {question.answerType === 'single_choice' && (
              <div
                className="plan-question-choice-list"
                role="radiogroup"
                aria-label={question.prompt}
                aria-required={question.required}
              >
                {question.options.map((option, optionIndex) => (
                  <button
                    key={option.id}
                    ref={(node) => { optionRefs.current[optionIndex] = node }}
                    type="button"
                    role="radio"
                    aria-checked={question.selectedOptionId === option.id}
                    tabIndex={focusedAnswerIndex === optionIndex ? 0 : -1}
                    className={`plan-question-option${question.selectedOptionId === option.id ? ' is-selected' : ''}`}
                    onFocus={() => setFocusedAnswerIndex(optionIndex)}
                    onClick={() => selectSingleOption(option.id)}
                    onKeyDown={(event) => handleSingleKeyDown(event, option.id)}
                  >
                    <span className="plan-question-option-index" aria-hidden="true">
                      {question.selectedOptionId === option.id
                        ? <Check size={13} />
                        : optionIndex + 1}
                    </span>
                    <span className="plan-question-option-copy">
                      <strong>{option.label}</strong>
                      {option.description && <small>{option.description}</small>}
                      {option.recommended && (
                        <span className="plan-question-option-recommended">{t('推荐')}</span>
                      )}
                    </span>
                  </button>
                ))}
              </div>
            )}
            {question.answerType === 'multiple_choice' && (
              <>
                <p className="plan-question-selection-hint">
                  {t('选择 {minimum} 至 {maximum} 项，自定义回答计作一项', {
                    minimum: question.minSelections,
                    maximum: multipleSelectionMaximum(question),
                  })}
                </p>
                <div className="plan-question-choice-list" role="group" aria-label={question.prompt}>
                  {question.options.map((option, optionIndex) => {
                    const selected = question.selectedOptionIds.includes(option.id)
                    const atLimit = multipleSelectionCount(question) >= multipleSelectionMaximum(question)
                    const disabled = !selected && atLimit
                    return (
                      <label
                        key={option.id}
                        className={`plan-question-option${selected ? ' is-selected' : ''}${disabled ? ' is-disabled' : ''}`}
                      >
                        <input
                          ref={(node) => { optionRefs.current[optionIndex] = node }}
                          className="plan-question-native-control"
                          type="checkbox"
                          checked={selected}
                          disabled={disabled}
                          onChange={() => toggleMultipleOption(option.id)}
                        />
                        <span className="plan-question-option-index" aria-hidden="true">
                          {selected ? <Check size={13} /> : optionIndex + 1}
                        </span>
                        <span className="plan-question-option-copy">
                          <strong>{option.label}</strong>
                          {option.description && <small>{option.description}</small>}
                        </span>
                      </label>
                    )
                  })}
                </div>
              </>
            )}
            {(question.answerType === 'text' || customAnswerAllowed) && (
              <label className={`plan-question-custom${customAnswer ? ' is-active' : ''}${customAnswerDisabled ? ' is-disabled' : ''}`}>
                <span className="visually-hidden">{t('自定义回答：{question}', { question: question.prompt })}</span>
                <span className="plan-question-option-index" aria-hidden="true">
                  <MessageSquareText size={13} />
                </span>
                <textarea
                  ref={textAnswerRef}
                  id={`plan-question-text-${interaction.interruptId}-${question.id}`}
                  rows={1}
                  value={customAnswer}
                  placeholder={t('输入你的答案')}
                  disabled={customAnswerDisabled}
                  onChange={(event) => {
                    resizeTextAnswer(event.currentTarget)
                    updateTextValue(event.currentTarget.value)
                  }}
                  onKeyDown={continueFromText}
                />
              </label>
            )}
            {question.answerType === 'date' && (
              <div className={`plan-question-date${question.date ? ' is-active' : ''}`}>
                <span className="plan-question-option-index" aria-hidden="true">
                  <CalendarDays size={13} />
                </span>
                <span className="plan-question-date-copy">
                  <span className="plan-question-date-label">{t('选择日期')}</span>
                  <DatePicker
                    ref={dateAnswerRef}
                    controlSize="xs"
                    value={question.date ?? ''}
                    label={t('日期回答：{question}', { question: question.prompt })}
                    onChange={(value) => {
                      updateQuestion((current) => current.answerType === 'date'
                        ? { ...current, date: value, skipped: false }
                        : current)
                    }}
                  />
                </span>
              </div>
            )}
            {question.answerType === 'time' && (
              <div className={`plan-question-date plan-question-time${question.time ? ' is-active' : ''}`}>
                <span className="plan-question-option-index" aria-hidden="true">
                  <Clock3 size={13} />
                </span>
                <span className="plan-question-date-copy">
                  <span className="plan-question-time-copy">
                    <span className="plan-question-date-label">{t('选择时间')}</span>
                    {(question.minimum || question.maximum) && (
                      <small>{t('允许范围：{minimum}–{maximum}', {
                        minimum: question.minimum ?? '00:00',
                        maximum: question.maximum ?? '23:59',
                      })}</small>
                    )}
                  </span>
                  <TimePicker
                    ref={timeAnswerRef}
                    controlSize="xs"
                    value={question.time ?? ''}
                    label={t('时间回答：{question}', { question: question.prompt })}
                    min={question.minimum ?? undefined}
                    max={question.maximum ?? undefined}
                    onChange={(value) => {
                      updateQuestion((current) => current.answerType === 'time'
                        ? { ...current, time: value, skipped: false }
                        : current)
                    }}
                  />
                </span>
              </div>
            )}
            {question.answerType === 'datetime' && (
              <div className={`plan-question-date plan-question-time plan-question-datetime${question.dateTime ? ' is-active' : ''}`}>
                <span className="plan-question-option-index" aria-hidden="true">
                  <CalendarClock size={13} />
                </span>
                <span className="plan-question-date-copy">
                  <span className="plan-question-time-copy">
                    <span className="plan-question-date-label">{t('选择日期和时间')}</span>
                  </span>
                  <PlanDateTimeControl
                    ref={dateTimeAnswerRef}
                    value={question.dateTime}
                    prompt={question.prompt}
                    minimum={question.minimum ?? undefined}
                    maximum={question.maximum ?? undefined}
                    onChange={(value) => {
                      updateQuestion((current) => current.answerType === 'datetime'
                        ? { ...current, dateTime: value, skipped: false }
                        : current)
                    }}
                  />
                </span>
              </div>
            )}
          </div>
        </div>

        <OverlayScrollbar viewportRef={bodyRef} />
        <footer className="plan-question-composer-footer">
          <div className="plan-question-composer-pager">
            <IconButton
              size="sm"
              className="plan-question-pager-button"
              label={t('浏览上一题')}
              icon={<ChevronLeft size={16} />}
              disabled={activeIndex === 0}
              onClick={() => setActiveQuestion(activeIndex - 1)}
            />
            <span>{activeIndex + 1} / {interaction.questions.length}</span>
            <IconButton
              size="sm"
              className="plan-question-pager-button"
              label={t('浏览下一题')}
              icon={<ChevronRight size={16} />}
              disabled={!canAdvance}
              onClick={advanceQuestion}
            />
          </div>
          <p className="plan-question-composer-feedback" role="status">
            {interaction.error ?? ''}
          </p>
          <div className="plan-question-composer-actions">
            {!question.required && (
              <Button size="sm" shape="capsule" variant="secondary" onClick={skipQuestion}>
                {t('跳过本题')}
              </Button>
            )}
            {hasNextQuestion ? (
              <Button
                size="sm"
                shape="capsule"
                variant="primary"
                disabled={!canAdvance}
                onClick={advanceQuestion}
              >
                {t('下一题')}
              </Button>
            ) : (
              <Button size="sm" shape="capsule" variant="primary" onClick={submit}>
                {t('提交')}
              </Button>
            )}
          </div>
        </footer>
      </>
    </PlanInteractionCard>
  )
}
