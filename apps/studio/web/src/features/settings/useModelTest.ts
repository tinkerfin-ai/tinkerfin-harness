import { useEffect, useRef, useState } from 'react'
import { requestJson } from '../../api/shared/http'
import type { ToastKind } from '../../components/ui/ToastViewport'
import { useI18n } from '../../i18n'
import { modelTestMessages } from './modelTestMessages'
import type { ModelSettings } from './useModelSettings'

export type ModelTestKind = 'basic' | 'text' | 'vision' | 'image'
export type ModelTestConfiguration = ModelSettings
export interface ModelTestResult {
  kind: ModelTestKind
  outcome: 'success' | 'failed' | 'inconclusive'
  elapsed_ms: number
  code: string
  text: string | null
  image: { mime_type: string; data_base64: string } | null
}

/** 拥有当前草稿的一次测试，取消或编辑后拒绝旧响应，结果不持久化 */
export function useModelTest(onToast: (kind: ToastKind, message: string) => void) {
  const { t } = useI18n()
  const active = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const [running, setRunning] = useState<ModelTestKind>()
  const [result, setResult] = useState<ModelTestResult>()
  const [stale, setStale] = useState(false)
  useEffect(() => () => { generation.current += 1; active.current?.abort(); active.current = null }, [])
  const invalidate = () => {
    generation.current += 1
    active.current?.abort()
    active.current = null
    setRunning(undefined)
    setStale(true)
  }
  const reset = () => { invalidate(); setResult(undefined); setStale(false) }
  const cancel = () => { invalidate(); setResult(undefined) }
  const run = async (kind: ModelTestKind, configuration: ModelTestConfiguration) => {
    if (active.current) return
    const sequence = ++generation.current
    const controller = new AbortController()
    active.current = controller
    setRunning(kind); setResult(undefined); setStale(false)
    try {
      const response = await requestJson<ModelTestResult>('/api/models/configurations/test', {
        method: 'POST', body: { kind, configuration }, signal: controller.signal,
      })
      if (controller.signal.aborted || generation.current !== sequence) return
      if (response.outcome === 'failed') {
        onToast('error', t(modelTestMessages[response.code] ?? '测试失败，请重试'))
      } else {
        setResult(response)
      }
    } catch {
      // 请求异常由全局提示展示，测试按钮恢复后可再次运行
    } finally {
      if (active.current === controller) { active.current = null; setRunning(undefined) }
    }
  }
  return { running, result, stale, run, invalidate, reset, cancel }
}
