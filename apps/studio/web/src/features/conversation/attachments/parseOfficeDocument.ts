import { unzipSync, zipSync } from 'fflate'
import mammoth from 'mammoth'
import * as xlsx from 'xlsx'
import {
  DOCUMENT_PREVIEW_BYTES, DOCUMENT_PREVIEW_CELL_CHARACTERS, DOCUMENT_PREVIEW_COLUMNS,
  DOCUMENT_PREVIEW_ROWS, DocumentPreviewError, type OfficeFormat, type OfficePreview,
} from './documentPreview'

const ARCHIVE_BYTES = 32 * 1024 * 1024
const ARCHIVE_ENTRIES = 2_000
const DOCX_HTML_CHARACTERS = 500_000

/** 验证实际解压内容后重建容器，避免解析器再次读取未经限制的压缩内容 */
function boundedArchive(bytes: ArrayBuffer, requiredEntry: string): Uint8Array<ArrayBuffer> {
  if (bytes.byteLength > DOCUMENT_PREVIEW_BYTES) throw new DocumentPreviewError('size')
  let total = 0
  let count = 0
  const entries = unzipSync(new Uint8Array(bytes), {
    filter(entry) {
      // fflate 对 STORE 按 size 复制，对 DEFLATE 按 originalSize 分配空间
      // 必须在解包前拒绝 STORE 的矛盾长度，解包后检查无法限制已经发生的分配
      if (entry.compression === 0 && entry.size !== entry.originalSize) {
        throw new DocumentPreviewError('invalid')
      }
      total += entry.originalSize
      count += 1
      if (total > ARCHIVE_BYTES || count > ARCHIVE_ENTRIES) throw new DocumentPreviewError('size')
      return true
    },
  })
  if (!entries[requiredEntry] || !entries['[Content_Types].xml']) throw new DocumentPreviewError('invalid')
  if (Object.values(entries).reduce((sum, entry) => sum + entry.byteLength, 0) > ARCHIVE_BYTES) throw new DocumentPreviewError('size')
  return zipSync(entries, { level: 0 })
}

/** 文档只提供文字、表格和已保存的单元格值，不读取外部资源或执行公式 */
export async function parseOfficeDocument(bytes: ArrayBuffer, format: OfficeFormat): Promise<OfficePreview> {
  const archive = boundedArchive(bytes, format === 'docx' ? 'word/document.xml' : 'xl/workbook.xml')
  if (format === 'docx') {
    const result = await mammoth.convertToHtml({ arrayBuffer: archive.buffer }, {
      externalFileAccess: false,
      includeEmbeddedStyleMap: false,
      convertImage: mammoth.images.imgElement(async () => ({ src: '' })),
    })
    if (result.value.length > DOCX_HTML_CHARACTERS) throw new DocumentPreviewError('size')
    return { kind: 'docx', html: result.value }
  }
  const workbook = xlsx.read(archive, {
    type: 'array', sheets: 0, sheetRows: DOCUMENT_PREVIEW_ROWS + 1,
    cellFormula: false, cellHTML: false, cellStyles: false, bookVBA: false,
  })
  const name = workbook.SheetNames[0]
  const sheet = name ? workbook.Sheets[name] : undefined
  if (!name || !sheet) throw new DocumentPreviewError('invalid')
  const range = sheet['!ref'] ? xlsx.utils.decode_range(sheet['!ref']) : undefined
  if (!range) return { kind: 'xlsx', sheet: name, rows: [], columns: [], truncated: false }
  const fullRange = xlsx.utils.decode_range(sheet['!fullref'] ?? sheet['!ref']!)
  const rowCount = Math.min(range.e.r + 1, DOCUMENT_PREVIEW_ROWS)
  const columnCount = Math.min(range.e.c + 1, DOCUMENT_PREVIEW_COLUMNS)
  let truncated = fullRange.e.r + 1 > rowCount || fullRange.e.c + 1 > columnCount
  const rows = Array.from({ length: rowCount }, (_, row) => Array.from({ length: columnCount }, (_, column) => {
    const cell = sheet[xlsx.utils.encode_cell({ r: row, c: column })]
    const value = cell ? xlsx.utils.format_cell(cell) : ''
    if (value.length > DOCUMENT_PREVIEW_CELL_CHARACTERS) truncated = true
    return value.slice(0, DOCUMENT_PREVIEW_CELL_CHARACTERS)
  }))
  return { kind: 'xlsx', sheet: name, rows, columns: Array.from({ length: columnCount }, (_, column) => xlsx.utils.encode_col(column)), truncated }
}
