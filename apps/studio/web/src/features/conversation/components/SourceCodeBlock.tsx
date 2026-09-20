import { CodeText } from '../../../components/ui/CodeText'
import { useI18n } from '../../../i18n'
import { CopyCodeButton } from './CopyCodeButton'

/** 普通代码与无法成图的源码使用同一只读代码框 */
export function SourceCodeBlock({ source, language, isStreaming = false, label }: {
  source: string; language?: string; isStreaming?: boolean; label?: string
}) {
  const { t } = useI18n()
  return <figure className="markdown-code-block" aria-label={label ?? language ?? t('文本')}>
    <figcaption className="markdown-code-block__head">
      <span>{language || t('文本')}</span>
      <CopyCodeButton source={source} diagram={language?.toLowerCase() === 'mermaid'} />
    </figcaption>
    <pre role="region" aria-label={t('源码')} tabIndex={0}><CodeText language={language} isStreaming={isStreaming}>{source}</CodeText></pre>
  </figure>
}
