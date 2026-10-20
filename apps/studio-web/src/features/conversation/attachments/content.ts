import type { JsonObject } from '../../../types'

export interface Attachment extends JsonObject {
  id: string
  name: string
  mime_type: string
  size_bytes: number
}

export const isAttachment = (value: unknown): value is Attachment => {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  return (
    typeof item.id === 'string' &&
    Boolean(item.id) &&
    typeof item.name === 'string' &&
    typeof item.mime_type === 'string' &&
    typeof item.size_bytes === 'number' &&
    Number.isSafeInteger(item.size_bytes) &&
    item.size_bytes >= 0
  )
}

const attachmentFromBlock = (block: unknown): Attachment | undefined => {
    if (!block || typeof block !== 'object') return undefined
    const part = block as Record<string, unknown>
    const extras =
      part.extras && typeof part.extras === 'object'
        ? (part.extras as Record<string, unknown>)
        : undefined
    if ((part.type === 'image' || part.type === 'file') && isAttachment(extras?.attachment)) return extras.attachment
    if ((part.type === 'image' || part.type === 'document') && isAttachment(part.metadata)) return part.metadata
    return undefined
}

export const messageAttachments = (content: unknown): Attachment[] => {
  if (!Array.isArray(content)) return []
  return content.flatMap((block: unknown) => {
    const attachment = attachmentFromBlock(block)
    return attachment ? [structuredClone(attachment)] : []
  })
}

export const messageText = (content: unknown): string => {
  if (typeof content === 'string') return content
  if (!Array.isArray(content))
    return content == null ? '' : JSON.stringify(content, null, 2)
  return content
    .map((part: unknown) => {
      if (typeof part === 'string') return part
      if (!part || typeof part !== 'object') return ''
      const block = part as Record<string, unknown>
      if (attachmentFromBlock(block)) return ''
      return block.type === 'text' && typeof block.text === 'string'
        ? block.text
        : JSON.stringify(block)
    })
    .join('')
}

export const attachmentInput = (file: Attachment): JsonObject => ({
  type: file.mime_type.startsWith('image/') ? 'image' : 'document',
  source: {
    type: 'url',
    value: `attachment:${file.id}`,
    mimeType: file.mime_type,
  },
  metadata: file,
})
