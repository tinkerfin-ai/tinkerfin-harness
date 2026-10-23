export const DOCUMENT_PREVIEW_BYTES = 10 * 1024 * 1024
export const DOCUMENT_PREVIEW_CHARACTERS = 100_000
export const TEXT_PREVIEW_LINES = 200
export const DOCUMENT_PREVIEW_ROWS = 100
export const DOCUMENT_PREVIEW_COLUMNS = 20
export const DOCUMENT_PREVIEW_CELL_CHARACTERS = 500
export const DOCUMENT_PREVIEW_TIMEOUT = 15_000

export type DocumentFormat = 'text' | 'markdown' | 'pdf' | 'docx' | 'xlsx' | 'pptx'
export type OfficeFormat = Extract<DocumentFormat, 'docx' | 'xlsx'>
export type OfficePreview =
  | { kind: 'docx'; html: string }
  | { kind: 'xlsx'; sheet: string; columns: string[]; rows: string[][]; truncated: boolean }
export type PreviewFailure = 'invalid' | 'size' | 'timeout' | 'encoding'
export type OfficePreviewResponse = { content: OfficePreview } | { error: PreviewFailure }

export class DocumentPreviewError extends Error {
  constructor(readonly code: PreviewFailure) { super(code) }
}

export function documentFormat(mime: string): DocumentFormat | undefined {
  switch (mime) {
    case 'text/plain':
    case 'text/csv':
    case 'application/json':
    case 'text/html':
    case 'text/x-python': return 'text'
    case 'text/markdown': return 'markdown'
    case 'application/pdf': return 'pdf'
    case 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': return 'docx'
    case 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': return 'xlsx'
    case 'application/vnd.openxmlformats-officedocument.presentationml.presentation': return 'pptx'
    default: return undefined
  }
}

export function readTextPreview(bytes: ArrayBuffer): { kind: 'text'; text: string; truncated: boolean } {
  let text: string
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    throw new DocumentPreviewError('encoding')
  }
  if (text.includes('\0')) throw new DocumentPreviewError('invalid')
  const lines = text.split(/\r\n|\r|\n/, TEXT_PREVIEW_LINES + 1)
  const excerpt = lines.slice(0, TEXT_PREVIEW_LINES).join('\n')
  return {
    kind: 'text',
    text: excerpt.slice(0, DOCUMENT_PREVIEW_CHARACTERS),
    truncated: lines.length > TEXT_PREVIEW_LINES || excerpt.length > DOCUMENT_PREVIEW_CHARACTERS,
  }
}
