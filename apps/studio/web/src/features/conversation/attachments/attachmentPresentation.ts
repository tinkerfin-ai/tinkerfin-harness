import type { Attachment } from './content'

export type AttachmentFileKind =
  | 'archive'
  | 'code'
  | 'image'
  | 'markdown'
  | 'pdf'
  | 'sheet'
  | 'slides'
  | 'text'
  | 'unknown'
  | 'word'

const WORD_EXTENSIONS = new Set(['doc', 'docx', 'odt', 'rtf'])
const MARKDOWN_EXTENSIONS = new Set(['markdown', 'md', 'mdown', 'mkdn'])
const SHEET_EXTENSIONS = new Set(['csv', 'ods', 'xls', 'xlsx'])
const SLIDE_EXTENSIONS = new Set(['odp', 'pps', 'ppsx', 'ppt', 'pptx'])
const TEXT_EXTENSIONS = new Set(['log', 'text', 'txt'])
const ARCHIVE_EXTENSIONS = new Set(['7z', 'bz2', 'gz', 'rar', 'tar', 'tgz', 'zip'])
const CODE_EXTENSIONS = new Set([
  'css', 'go', 'html', 'ini', 'java', 'js', 'json', 'jsx', 'py', 'rs', 'sh',
  'sql', 'toml', 'ts', 'tsx', 'xml', 'yaml', 'yml',
])
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'heic', 'jpeg', 'jpg', 'png', 'svg', 'tif', 'tiff', 'webp'])

function attachmentExtension(name: string) {
  const segment = name.split('.').at(-1)?.trim().toLowerCase()
  return segment && segment !== name.toLowerCase() ? segment : ''
}

export function attachmentFileType(attachment: Attachment): {
  kind: AttachmentFileKind
  label: string
} {
  const extension = attachmentExtension(attachment.name)
  const mime = attachment.mime_type.toLowerCase()
  if (mime.startsWith('image/') || IMAGE_EXTENSIONS.has(extension))
    return { kind: 'image', label: extension ? extension.toUpperCase() : 'IMG' }
  if (mime === 'application/pdf' || extension === 'pdf')
    return { kind: 'pdf', label: 'PDF' }
  if (mime === 'text/markdown' || MARKDOWN_EXTENSIONS.has(extension))
    return { kind: 'markdown', label: 'MD' }
  if (
    mime.includes('wordprocessingml')
    || mime === 'application/msword'
    || WORD_EXTENSIONS.has(extension)
  ) return { kind: 'word', label: extension ? extension.toUpperCase() : 'DOC' }
  if (
    mime.includes('spreadsheetml')
    || mime === 'application/vnd.ms-excel'
    || SHEET_EXTENSIONS.has(extension)
  ) return { kind: 'sheet', label: extension ? extension.toUpperCase() : 'XLS' }
  if (
    mime.includes('presentationml')
    || mime === 'application/vnd.ms-powerpoint'
    || SLIDE_EXTENSIONS.has(extension)
  ) return { kind: 'slides', label: extension ? extension.toUpperCase() : 'PPT' }
  if (mime.includes('json') || mime.includes('javascript') || CODE_EXTENSIONS.has(extension))
    return { kind: 'code', label: extension ? extension.slice(0, 4).toUpperCase() : 'CODE' }
  if (mime.startsWith('text/') || TEXT_EXTENSIONS.has(extension))
    return { kind: 'text', label: extension ? extension.slice(0, 4).toUpperCase() : 'TXT' }
  if (mime.includes('zip') || mime.includes('compressed') || ARCHIVE_EXTENSIONS.has(extension))
    return { kind: 'archive', label: extension ? extension.slice(0, 4).toUpperCase() : 'ZIP' }
  return {
    kind: 'unknown',
    label: extension ? extension.slice(0, 4).toUpperCase() : 'FILE',
  }
}

export function formatAttachmentSize(sizeBytes: number) {
  if (sizeBytes < 1024) return `${sizeBytes} B`
  const kibibytes = sizeBytes / 1024
  if (kibibytes < 1024) return `${kibibytes.toFixed(1)} KiB`
  return `${(kibibytes / 1024).toFixed(1)} MiB`
}
