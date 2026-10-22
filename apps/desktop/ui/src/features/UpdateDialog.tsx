import { ArrowDownToLine, Check, RotateCw, TriangleAlert } from 'lucide-react'
import { useLayoutEffect, useRef } from 'react'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { useI18n } from '../i18n'
import type { UpdatePhase, UpdateState } from '../state'
import { useAction } from './useAction'

export function UpdateDialog({ update, completed }: { update: UpdateState; completed: () => void }) {
  const { t } = useI18n()
  const titleRef = useRef<HTMLHeadingElement>(null)
  const action = useAction()
  const { phase, percent, currentVersion, targetVersion } = update
  const open = phase !== 'idle', locked = phase === 'installing' || phase === 'restarting'
  const format = (key: string, version: string) => t(key).replace('{version}', version)
  const titles: Record<UpdatePhase, string> = {
    idle: '', checking: t('正在检查更新'), available: format('检测到新版本 {version}', targetVersion), latest: t('已是最新版本'),
    'check-error': t('检查更新失败'), downloading: t('正在下载更新'), 'download-error': t('下载失败'),
    verifying: t('正在校验安装包'), 'verify-error': t('校验失败'), waiting: t('等待本机操作完成'), installing: t('正在安装更新'),
    'install-error': t('安装失败'), restarting: t('正在重新启动'), 'restart-error': t('未能重新启动'), success: format('已更新至 {version}', currentVersion),
  }
  const descriptions: Partial<Record<UpdatePhase, string>> = {
    available: t('是否更新？更新后将重新启动'), latest: format('当前版本 {version}', currentVersion),
    'check-error': t('请检查网络后重试'), 'download-error': t('请检查网络后重试'),
    'verify-error': t('请重新下载安装包'), 'install-error': t('当前版本仍可使用'),
  }
  const busy = ['checking', 'downloading', 'verifying', 'waiting', 'installing', 'restarting'].includes(phase)
  const determinate = phase === 'downloading' || phase === 'installing'
  const warning = phase.endsWith('-error')
  const Icon = warning ? TriangleAlert : ['latest', 'success'].includes(phase) ? Check : phase === 'restarting' ? RotateCw : ArrowDownToLine

  useLayoutEffect(() => {
    if (!open) return
    const dialog = titleRef.current?.closest<HTMLElement>('[role="dialog"]')
    if (dialog && !dialog.contains(document.activeElement)) dialog.focus({ preventScroll: true })
  }, [open, phase])
  useLayoutEffect(() => {
    if (!open || !locked) return
    const backdrop = titleRef.current?.closest('.modal-backdrop')
    if (!backdrop) return
    const keepFocus = (event: Event) => { if (event.target === backdrop) event.preventDefault() }
    backdrop.addEventListener('mousedown', keepFocus)
    return () => backdrop.removeEventListener('mousedown', keepFocus)
  }, [open, locked])

  const close = () => {
    if (locked) return
    void action.run({ type: 'update-cancel' }, result => { if (result.ok && phase === 'success') completed() })
  }
  return <Dialog open={open} title={titles[phase]} closeDisabled={locked || action.pending} className="desktop-update-dialog" onClose={close}
    header={<header className={`desktop-update-heading${warning ? ' is-warning' : ''}`}><Icon size={20} strokeWidth={1.7} aria-hidden="true" /><h2 ref={titleRef} aria-live="polite">{titles[phase]}</h2></header>}>
    {descriptions[phase] && <p className="desktop-update-copy">{descriptions[phase]}</p>}
    {busy && <div className="desktop-update-progress">
      {determinate && <div className="desktop-update-percent" aria-hidden="true">{Math.floor(percent)}%</div>}
      <div className={`desktop-update-track${!determinate ? ' is-indeterminate' : ''}`} role="progressbar" aria-label={determinate ? t(phase === 'downloading' ? '下载进度' : '安装进度') : titles[phase]}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={determinate ? percent : undefined}><span style={{ width: determinate ? `${percent}%` : '36%' }} /></div>
    </div>}
    <div className="desktop-update-actions">
      {!locked && <Button key="cancel" variant="text" disabled={action.pending} onClick={close}>{t(phase === 'success' ? '完成' : phase === 'latest' ? '关闭' : phase === 'restart-error' ? '稍后' : '取消')}</Button>}
      {phase === 'available' && <Button key="confirm" variant="primary" loading={action.pending} onClick={() => void action.run({ type: 'update-confirm' })}>{t('立即更新')}</Button>}
      {warning && <Button key="retry" variant="primary" loading={action.pending} onClick={() => void action.run({ type: 'update-retry' })}>{t(phase === 'verify-error' ? '重新下载' : phase === 'restart-error' ? '重新启动' : '重试')}</Button>}
    </div>
  </Dialog>
}
