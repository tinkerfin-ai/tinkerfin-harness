export interface AgentModelCatalogItem {
  modelId: string
  displayName: string
  connectionId: string
  connectionDisplayName: string
  imageSupport: 'supported' | 'unsupported' | 'unknown'
  reasoningEnabled: boolean
  isDefault: boolean
}

export interface AgentModelCatalog {
  items: AgentModelCatalogItem[]
  defaultModelId?: string | null
}
