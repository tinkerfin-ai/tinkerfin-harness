import { Check, Copy, TriangleAlert } from 'lucide-react'
import { IconButton } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import { useCopyFeedback } from './copyFeedback'

export function CopyCodeButton({ source, diagram = false }: { source: string; diagram?: boolean }) {
  const { t } = useI18n()
  const { state, copy } = useCopyFeedback()
  const label = state === 'copied' ? t('已复制') : state === 'failed' ? t('复制失败') : diagram ? t('复制源码') : t('复制')
  const icon = state === 'copied' ? <Check size={16} /> : state === 'failed' ? <TriangleAlert size={16} /> : <Copy size={16} />
  return <IconButton type="button" variant="ghost" size="lg" label={label} tooltip={label} icon={icon} disabled={!source} onClick={() => void copy(source)} />
}
