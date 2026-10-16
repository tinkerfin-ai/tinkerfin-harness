import { requestJson } from '../shared/http'
import type { AgentModelCatalog } from './types'

export const fetchModelCatalog = (
  signal?: AbortSignal,
): Promise<AgentModelCatalog> => requestJson<AgentModelCatalog>('/api/models', {
  signal,
})
