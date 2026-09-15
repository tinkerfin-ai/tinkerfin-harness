import { CheckCircle2, CircleAlert, Clock3, XCircle } from 'lucide-react'
import { Button } from '../../components/ui'
import { useI18n } from '../../i18n'
import { modelTestMessages } from './modelTestMessages'
import type { ModelTestKind, ModelTestResult } from './useModelTest'


export interface ModelTestPanelProps {
  purpose: 'chat' | 'image'
  disabled: boolean
  running?: ModelTestKind
  result?: ModelTestResult
  stale: boolean
  onRun: (kind: ModelTestKind) => void
  onCancel: () => void
}

/** 将基础连通性与真实模型能力分开展示，不把接口返回等同于识别正确 */
export function ModelTestPanel({ purpose, disabled, running, result, stale, onRun, onCancel }: ModelTestPanelProps) {
  const { t } = useI18n()
  const Status = result?.outcome === 'success' ? CheckCircle2 : result?.outcome === 'failed' ? XCircle : CircleAlert
  return <section className="settings-models__test" aria-label={t('模型测试')}>
    <div className="settings-models__test-row">
      <div><strong>{t('基础检查')}</strong><p>{t('检查服务地址、密钥和模型名称')}</p></div>
      <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'basic'} onClick={() => onRun('basic')}>{t('检查连接')}</Button>
    </div>
    <div className="settings-models__test-row">
      <div><strong>{t('能力测试')}</strong><p>{t('实际发送一次请求，可能产生费用')}</p></div>
      <div className="settings-models__test-buttons">
        {purpose === 'chat' ? <>
          <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'text'} onClick={() => onRun('text')}>{t('文字回复')}</Button>
          <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'vision'} onClick={() => onRun('vision')}>{t('图片理解')}</Button>
        </> : <Button type="button" size="xs" disabled={disabled || Boolean(running)} loading={running === 'image'} onClick={() => onRun('image')}>{t('生成测试图片')}</Button>}
      </div>
    </div>
    {running && <div className="settings-models__test-pending" role="status"><span>{t('测试进行中…')}</span><Button type="button" variant="text" onClick={onCancel}>{t('取消等待')}</Button></div>}
    {result && <div className={`settings-models__test-result${stale ? ' is-stale' : ''}`} role="status" data-outcome={result.outcome}>
      <div className="settings-models__result-heading"><Status size={16} aria-hidden="true" /><strong>{t(modelTestMessages[result.code] ?? '测试已完成，请查看结果')}</strong><span><Clock3 size={12} aria-hidden="true" />{t('{seconds} 秒', { seconds: (result.elapsed_ms / 1000).toFixed(1) })}</span></div>
      {['models_unavailable', 'model_not_listed'].includes(result.code) && <p className="settings-models__hint">{t(purpose === 'image' ? '请点击“生成测试图片”，看看能否成功生成' : '请点击“文字回复”，看看能否收到回答')}</p>}
      {stale && <p>{t('配置已修改，此结果已过期，请重新测试')}</p>}
      {result.image && ['image/png', 'image/jpeg', 'image/webp'].includes(result.image.mime_type) && <img className="settings-models__test-image" src={`data:${result.image.mime_type};base64,${result.image.data_base64}`} alt={t(result.kind === 'vision' ? '图片理解测试图' : '模型生成的测试图片')} />}
      {result.kind === 'vision' && <p className="settings-models__hint">{t('预期内容：左侧红色方块，右侧蓝色方块')}</p>}
      {result.text && <p className="settings-models__test-reply">{result.text}</p>}
    </div>}
  </section>
}
