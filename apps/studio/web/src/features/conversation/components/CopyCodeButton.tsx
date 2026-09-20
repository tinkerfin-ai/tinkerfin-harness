import { Check, Copy, TriangleAlert } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { COPY_FEEDBACK_DURATION_MS } from './copyFeedback'

export function CopyCodeButton({ source, diagram = false }: { source: string; diagram?: boolean }) {
  const { t } = useI18n()
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)
  const attempt = useRef(0)
  useEffect(() => () => { attempt.current++; window.clearTimeout(timer.current) }, [])
  const copy = async () => {
    const id = ++attempt.current
    window.clearTimeout(timer.current)
    let next: 'copied' | 'failed' = 'copied'
    try { await navigator.clipboard.writeText(source) } catch { next = 'failed' }
    if (id !== attempt.current) return
    setState(next)
    timer.current = window.setTimeout(() => setState('idle'), COPY_FEEDBACK_DURATION_MS)
  }
  const label = state === 'copied' ? t('已复制') : state === 'failed' ? t('复制失败') : diagram ? t('复制源码') : t('复制')
  const icon = state === 'copied' ? <Check size={16} /> : state === 'failed' ? <TriangleAlert size={16} /> : <Copy size={16} />
  return <IconButton type="button" variant="ghost" size="lg" label={label} tooltip={label} icon={icon} disabled={!source} onClick={() => void copy()} />
}
