import { useEffect, useState } from 'react'
import { useResolvedTheme } from '../../../components/ui/useResolvedTheme'
import { DiagramError, renderDiagram } from './renderDiagram'
import type { DiagramErrorKind, RenderedDiagram } from './renderDiagram'

export function useDiagram(source: string, isStreaming: boolean) {
  const theme = useResolvedTheme()
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState<{
    source: string; theme: string; attempt: number; diagram?: RenderedDiagram; error?: DiagramErrorKind
  }>()
  useEffect(() => {
    if (isStreaming || !source.trim()) return
    const controller = new AbortController()
    void renderDiagram(source, controller.signal).then(diagram => {
      if (!controller.signal.aborted) setResult({ source, theme, attempt, diagram })
    }).catch(error => {
      if (!controller.signal.aborted) setResult({ source, theme, attempt, error: error instanceof DiagramError ? error.kind : 'render' })
    })
    return () => controller.abort()
  }, [source, theme, isStreaming, attempt])
  const current = result?.source === source && result.theme === theme && result.attempt === attempt && !isStreaming ? result : undefined
  return { ...current, diagram: current?.diagram ?? (!isStreaming && result?.source === source ? result.diagram : undefined), retry: () => setAttempt(value => value + 1) }
}
