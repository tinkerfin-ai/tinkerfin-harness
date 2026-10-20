import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { fetchModelCatalog } from '../../api/models/client'
import type { AgentModelCatalogItem } from '../../api/models/types'

export type ModelCatalogStatus = 'loading' | 'ready' | 'empty' | 'error'

export function useModelCatalog() {
  const [models, setModels] = useState<AgentModelCatalogItem[]>([])
  const [catalogDefaultModelId, setCatalogDefaultModelId] = useState('')
  const [status, setStatus] = useState<ModelCatalogStatus>('loading')
  const [version, setVersion] = useState(0)
  const activeRequest = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    activeRequest.current = controller
    setStatus('loading')
    void fetchModelCatalog(controller.signal).then((catalog) => {
      if (controller.signal.aborted) return
      setModels(catalog.items)
      setCatalogDefaultModelId(catalog.defaultModelId ?? '')
      // 空目录是可恢复的配置状态，必须与请求失败分开呈现
      setStatus(catalog.items.length === 0 ? 'empty' : 'ready')
    }).catch(() => {
      if (!controller.signal.aborted) setStatus('error')
    })
    return () => {
      controller.abort()
      if (activeRequest.current === controller) activeRequest.current = null
    }
  }, [version])

  const modelIds = useMemo(() => models.map((model) => model.modelId), [models])
  const defaultModelId = useMemo(() => (
    models.some((model) => model.modelId === catalogDefaultModelId)
      ? catalogDefaultModelId
      : models.find((model) => model.isDefault)?.modelId ?? models[0]?.modelId ?? ''
  ), [catalogDefaultModelId, models])
  const retry = useCallback(() => {
    activeRequest.current?.abort()
    setStatus('loading')
    setVersion((current) => current + 1)
  }, [])

  return {
    status,
    modelIds,
    models,
    defaultModelId,
    retry,
  }
}
