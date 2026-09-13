export const DOCUMENT_PREVIEW_BYTES = 10 * 1024 * 1024
export const DOCUMENT_PREVIEW_CHARACTERS = 100_000
export const DOCUMENT_PREVIEW_ROWS = 100
export const DOCUMENT_PREVIEW_COLUMNS = 20
export const DOCUMENT_PREVIEW_CELL_CHARACTERS = 500
export const DOCUMENT_PREVIEW_TIMEOUT = 15_000

export type DocumentFormat = 'markdown' | 'pdf' | 'docx' | 'xlsx'
export type OfficeFormat = Extract<DocumentFormat, 'docx' | 'xlsx'>
export type OfficePreview =
  | { kind: 'docx'; html: string }
  | { kind: 'xlsx'; sheet: string; columns: string[]; rows: string[][]; truncated: boolean }
export type PreviewFailure = 'invalid' | 'size' | 'timeout'
export type OfficePreviewResponse = { content: OfficePreview } | { error: PreviewFailure }

export class DocumentPreviewError extends Error {
  constructor(readonly code: PreviewFailure) { super(code) }
}

export function documentFormat(mime: string): DocumentFormat | undefined {
  switch (mime) {
    case 'text/markdown': return 'markdown'
    case 'application/pdf': return 'pdf'
    case 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': return 'docx'
    case 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': return 'xlsx'
    default: return undefined
  }
}

