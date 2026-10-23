import type { Message } from '../../../types'

type DeliveryResult =
  | { file_path: string; name: string; status: 'delivered'; attachment_id: string }
  | { file_path: string; name: string; status: 'failed'; error: string }

export function isDeliveryTool(name?: string): boolean {
  return name === 'deliver_files' || name === 'computer_deliver_files'
}

export function readDeliveryResults(message: Message): DeliveryResult[] | null {
  if (!isDeliveryTool(message.meta?.toolName) || !message.meta?.result || message.meta.status === 'running') return null
  try {
    const value: unknown = JSON.parse(message.meta.result)
    if (!value || typeof value !== 'object' || !('results' in value) || !Array.isArray(value.results)) return null
    const results: DeliveryResult[] = []
    for (const item of value.results) {
      if (!item || typeof item !== 'object' || typeof item.file_path !== 'string' || typeof item.name !== 'string') return null
      if (item.status === 'delivered' && typeof item.attachment_id === 'string') {
        results.push({ file_path: item.file_path, name: item.name, status: 'delivered', attachment_id: item.attachment_id })
      } else if (item.status === 'failed' && typeof item.error === 'string') {
        results.push({ file_path: item.file_path, name: item.name, status: 'failed', error: item.error })
      } else return null
    }
    return results.length ? results : null
  } catch {
    return null
  }
}
