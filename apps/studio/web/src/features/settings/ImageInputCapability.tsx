import { useEffect, useState } from 'react'
import { Button } from '../../components/ui'
import { requestJson } from '../../api/shared/http'
import { ModelChoice } from './ModelChoice'
import { useI18n } from '../../i18n'
import type { ImageInputCapability, ImageSupport, ModelConnection } from './useModelSettings'

function imageInputLabel(support: ImageSupport) {
  return support === 'supported' ? '支持图片输入' : support === 'unsupported' ? '不支持图片输入' : '图片输入暂未识别'
}

export function ImageInputStatus({ capability }: { capability: ImageInputCapability }) {
  const { t } = useI18n()
  return <span>{t(imageInputLabel(capability.effective))}{capability.source === 'manual' ? ` · ${t('手动指定')}` : ''}</span>
}

type Lookup = { key: string; capability: ImageInputCapability } | { key: string; failed: true }

/** 查询只读能力资料；失效查询取消，结果只属于发起时的连接与输入 */
function useInputCapability(connection: ModelConnection, modelName: string, requestedName: string, declaration: ImageSupport) {
  const [lookup, setLookup] = useState<Lookup>()
  const [revision, setRevision] = useState(0)
  const name = modelName.trim()
  const ready = name.length > 0 && name === requestedName
  const key = JSON.stringify([connection.connection_id, connection.provider_id, connection.api_type, connection.base_url, name, declaration, revision])
  useEffect(() => {
    if (!ready) return
    const controller = new AbortController()
    void requestJson<{ image_input_capability: ImageInputCapability }>('/api/models/input-capabilities', {
      method: 'POST', body: { connection_id: connection.connection_id, model_name: name, image_support: declaration },
      signal: controller.signal, suppressGlobalError: true,
    }).then(result => {
      if (!controller.signal.aborted) setLookup({ key, capability: result.image_input_capability })
    }).catch(() => {
      if (!controller.signal.aborted) setLookup({ key, failed: true })
    })
    return () => controller.abort()
  }, [connection.connection_id, name, declaration, ready, key])
  return { ready, lookup: lookup?.key === key && ready ? lookup : undefined, retry: () => setRevision(value => value + 1) }
}

export function ImageInputCapabilityField({ connection, modelName, requestedName, declaration, onChange, disabled = false }: {
  connection: ModelConnection; modelName: string; requestedName: string; declaration: ImageSupport; onChange: (value: ImageSupport) => void; disabled?: boolean
}) {
  const { t } = useI18n()
  const { ready, lookup, retry } = useInputCapability(connection, modelName, requestedName, declaration)
  const valueText = (value: ImageSupport) => {
    if (value !== 'unknown') return t(value === 'supported' ? '支持' : '不支持')
    if (lookup && 'failed' in lookup) return t('自动 · 查询失败')
    if (!ready) return t('自动识别')
    if (!lookup) return t('自动 · 识别中')
    return t(lookup.capability.effective === 'supported' ? '自动 · 支持' : lookup.capability.effective === 'unsupported' ? '自动 · 不支持' : '自动 · 未识别')
  }
  return <div className="settings-models__capability">
    <ModelChoice label={t('图片输入')} value={declaration} options={['unknown', 'supported', 'unsupported']} layout="row" disabled={disabled}
      text={value => t(value === 'unknown' ? '自动识别' : imageInputLabel(value))} valueText={valueText} onChange={onChange} />
    {lookup && 'failed' in lookup && <div className="settings-models__capability-feedback" role="status"><span>{t('能力查询失败，配置仍可保存')}</span><Button type="button" variant="text" size="sm" disabled={disabled} onClick={retry}>{t('重试')}</Button></div>}
  </div>
}
