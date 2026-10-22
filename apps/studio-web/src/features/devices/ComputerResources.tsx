import { ArrowUpRight, Check, ChevronDown, Laptop, Settings2, ShieldAlert, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { Button, IconButton, Surface, Tooltip } from '../../components/ui'
import { restoreFocus } from '../../components/ui/focus'
import { useI18n } from '../../i18n'
import type { AccessMode } from '../../types'
import type { useDevices } from './useDevices'
import './devices.css'

/** 本机状态与本会话确认选择共用入口，设备配置保留在个人设置中 */
export function ComputerResources({ devices, value, onChange, disabled, onOpenSettings }: {
  devices: ReturnType<typeof useDevices>
  value: AccessMode
  onChange: (value: AccessMode) => void
  disabled: boolean
  onOpenSettings: (trigger: HTMLElement | null) => void
}) {
  const { t } = useI18n()
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const settingsButton = useRef<HTMLButtonElement>(null)
  const selectedChoice = useRef<HTMLInputElement>(null)
  const nameRef = useRef<HTMLSpanElement>(null)
  const pointerOpen = useRef(false)
  const [visible, setVisible] = useState(false)
  const [position, setPosition] = useState<CSSProperties>({ left: 0, top: 0 })
  const device = devices.devices.find(item => !item.revoked)
  const state = devices.status === 'ready' ? device ? device.online ? 'online' : 'offline' : 'unconfigured' : devices.status
  const name = device?.name ?? t('本地设备')
  const connected = state === 'online'
  const expandable = connected || state === 'offline'
  const status = t(connected ? '已连接' : state === 'offline' ? '未连接' : state === 'unconfigured' ? '去连接' : state === 'error' ? '加载失败' : '加载中')
  const modeLabel = t(value === 'full' ? '自动执行' : '修改前确认')

  const dismiss = useCallback((returnFocus: boolean) => {
    panel.current?.hidePopover()
    setVisible(false)
    if (returnFocus) restoreFocus(trigger.current, { preventScroll: true })
  }, [])
  useEffect(() => { if (!expandable && visible) dismiss(false) }, [expandable, visible, dismiss])
  const fit = useCallback(() => {
    const anchor = trigger.current?.getBoundingClientRect()
    const bounds = panel.current?.getBoundingClientRect()
    if (!anchor || !bounds) return
    const tokens = getComputedStyle(document.documentElement)
    const inset = parseFloat(tokens.getPropertyValue('--space-4')) || 0
    const gap = parseFloat(tokens.getPropertyValue('--space-2')) || 0
    setPosition({ left: Math.max(inset, Math.min(anchor.left, innerWidth - bounds.width - inset)),
      top: Math.max(inset, Math.min(anchor.bottom + gap, innerHeight - bounds.height - inset)) })
  }, [])
  useLayoutEffect(() => {
    if (!visible) return
    const observer = new ResizeObserver(fit)
    if (panel.current) observer.observe(panel.current)
    if (trigger.current) observer.observe(trigger.current)
    const element = panel.current
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !(event.target instanceof Node) || !element?.contains(event.target)) return
      event.preventDefault()
      event.stopPropagation()
      dismiss(true)
    }
    const blur = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && !element?.contains(event.relatedTarget) && !trigger.current?.contains(event.relatedTarget)) dismiss(false)
    }
    // 非滚动浮层消费滚轮，避免背景会话接管；Ctrl+滚轮仍保留浏览器缩放
    const containWheel = (event: WheelEvent) => {
      if (event.ctrlKey) return
      event.preventDefault()
      event.stopPropagation()
    }
    document.addEventListener('keydown', escape, true)
    element?.addEventListener('focusout', blur)
    element?.addEventListener('wheel', containWheel, { passive: false })
    window.addEventListener('resize', fit)
    window.addEventListener('scroll', fit, true)
    return () => {
      observer.disconnect()
      document.removeEventListener('keydown', escape, true)
      element?.removeEventListener('focusout', blur)
      element?.removeEventListener('wheel', containWheel)
      window.removeEventListener('resize', fit)
      window.removeEventListener('scroll', fit, true)
    }
  }, [visible, fit, dismiss])
  const openSettings = () => { dismiss(false); onOpenSettings(trigger.current) }
  return <span className="local-resources computer-resources">
    <Tooltip content={name} overflowOnly overflowRef={nameRef}>
      <Button ref={trigger} type="button" variant="ghost" size="sm" className="lr-trigger"
        aria-label={t('本机资源')} aria-description={`${name}，${status}${connected ? `，${modeLabel}` : ''}`}
        aria-haspopup={expandable ? 'dialog' : undefined} aria-expanded={expandable ? visible : undefined}
        aria-controls={expandable ? id : undefined} popoverTarget={expandable ? id : undefined}
        onPointerDown={() => { pointerOpen.current = visible }}
        onClick={event => {
          event.preventDefault()
          if (!expandable) { openSettings(); return }
          if (event.detail ? pointerOpen.current : visible) dismiss(true)
          else {
            panel.current?.showPopover()
            fit()
            setVisible(true)
            if (connected && !disabled) selectedChoice.current?.focus({ preventScroll: true })
            else settingsButton.current?.focus({ preventScroll: true })
          }
        }}>
        <span className="lr-trigger-icon"><Laptop size={16} aria-hidden="true" /><span className={`lr-dot${connected ? '' : state === 'offline' || state === 'error' ? ' lr-dot-offline' : ' lr-dot-unconfigured'}`} /></span>
        <span ref={nameRef} className="lr-trigger-name">{name}</span>
        <span className="lr-trigger-divider" aria-hidden="true" />
        <span className="lr-trigger-mode">{connected ? modeLabel : status}</span>
        {expandable ? <ChevronDown className="lr-chevron" aria-hidden="true" /> : <ArrowUpRight className="lr-arrow" aria-hidden="true" />}
      </Button>
    </Tooltip>
    <div ref={panel} id={id} className="computer-resources__popover" popover="auto" role="dialog"
      aria-label={t('本机资源')} aria-hidden={!visible} style={position}
      onToggle={event => setVisible(event.newState === 'open')}>
      <Surface elevation={2} className={`lr-panel${connected ? '' : ' lr-offline-panel'}`}>
        <div className="lr-device">
          <Tooltip content={name} overflowOnly><span className="lr-device-name">{name}</span></Tooltip>
          <div className="lr-device-meta">
            <span className={`lr-status${connected ? '' : ' lr-status-offline'}`} role="status"><span className={`lr-dot${connected ? '' : ' lr-dot-offline'}`} aria-hidden="true" />{status}</span>
            <IconButton ref={settingsButton} type="button" variant="ghost" className="lr-icon-button" label={t('打开本地设备设置')} icon={<Settings2 size={17} />} onClick={openSettings} />
          </div>
        </div>
        {connected && <>
          <div className="lr-separator" aria-hidden="true" />
          <fieldset className="lr-options" disabled={disabled}>
            <legend>{t('本机操作权限')}</legend>
            {(['write_approval', 'full'] as const).map(mode => <label key={mode} className="lr-option">
              <span className={`lr-option-icon${mode === 'full' ? ' lr-option-icon-alert' : ''}`}>{mode === 'full' ? <ShieldAlert size={18} aria-hidden="true" /> : <ShieldCheck size={18} aria-hidden="true" />}</span>
              <span className="lr-option-copy"><span className="lr-option-title">{t(mode === 'write_approval' ? '修改前确认' : '自动执行')}</span><span className="lr-option-description">{t(mode === 'write_approval' ? '修改本机内容前，先由你确认' : '在已授权范围内自动执行')}</span></span>
              <input ref={value === mode ? selectedChoice : undefined} className="lr-mode-input" type="radio" name={id} value={mode} checked={value === mode} aria-label={t(mode === 'write_approval' ? '修改前确认' : '自动执行')} onChange={() => onChange(mode)} onClick={() => dismiss(true)} />
              <span className="lr-check"><Check size={16} aria-hidden="true" /></span>
            </label>)}
          </fieldset>
        </>}
      </Surface>
    </div>
  </span>
}
