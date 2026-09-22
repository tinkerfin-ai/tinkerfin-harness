import { useEffect, useRef } from 'react'

import type { ComposerSuggestionGroup } from '../composerSuggestions'
import { useI18n, type TranslationKey } from '../../../i18n'

export function ComposerSuggestionMenu({
  id,
  groups,
  activeId,
  onPick,
  onDismiss,
}: {
  id: string
  groups: readonly ComposerSuggestionGroup[]
  activeId?: string
  onPick: (id: string) => void
  onDismiss: () => void
}) {
  const { t } = useI18n()
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handleOutsideMouseDown = (event: MouseEvent) => {
      if (!(event.target instanceof Node)) return
      if (menuRef.current?.contains(event.target)) return
      const composer = menuRef.current?.closest('.composer')
      if (composer?.contains(event.target)) return
      const target = event.target instanceof Element ? event.target : null
      const movesFocus = Boolean(target?.closest(
        'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [contenteditable="true"], [role="button"], [role="checkbox"], [role="combobox"], [role="link"], [role="menuitem"], [role="option"], [role="radio"], [role="switch"], [role="tab"], [role="textbox"]',
      ))
      if (!movesFocus) event.preventDefault()
      onDismiss()
    }
    document.addEventListener('mousedown', handleOutsideMouseDown, true)
    return () => document.removeEventListener('mousedown', handleOutsideMouseDown, true)
  }, [onDismiss])

  return (
    <div
      ref={menuRef}
      id={id}
      className="composer-suggestion-menu"
      role="listbox"
      aria-label={t('命令和技能建议')}
      onWheel={(event) => event.stopPropagation()}
    >
      <div className="composer-suggestion-viewport">
        {groups.map((group) => (
          <div key={group.id} className="ui-listbox-group" role="group" aria-label={t(group.label as TranslationKey)}>
            <div className="ui-listbox-group-label" role="presentation">
              {group.id === 'command' ? t('指令（{count}）', { count: group.items.length }) : t(group.label as TranslationKey)}
            </div>
            {group.items.map((item) => {
              const itemId = `${group.id}-${item.id}`
              return (
                <button
                  key={itemId}
                  id={`${id}-${itemId}`}
                  type="button"
                  role="option"
                  aria-selected={activeId === itemId}
                  aria-disabled={item.disabled}
                  disabled={item.disabled}
                  className={`composer-suggestion-item${activeId === itemId ? ' is-active' : ''}`}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => onPick(itemId)}
                >
                  <span className="composer-suggestion-name">{item.id === 'skills-unavailable' ? t(item.name as TranslationKey) : item.name}</span>
                  <span className="composer-suggestion-description">{t(item.description as TranslationKey)}</span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
