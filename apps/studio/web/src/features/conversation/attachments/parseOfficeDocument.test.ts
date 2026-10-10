import { strToU8, unzipSync, zipSync } from 'fflate'
import { Buffer } from 'node:buffer'
import { describe, expect, it, vi } from 'vitest'
import * as XLSX from 'xlsx'
import { DOCUMENT_PREVIEW_BYTES } from './documentPreview'
import { parseOfficeDocument } from './parseOfficeDocument'

// Node 使用相同 Mammoth 源码，以其公开 Buffer 输入核验转换；浏览器入口由构建与浏览器测试核验
vi.mock('mammoth', async (importOriginal) => {
  const { default: mammoth } = await importOriginal<{ default: typeof import('mammoth') }>()
  return { default: {
    ...mammoth,
    convertToHtml: (input: { arrayBuffer: ArrayBuffer }, options: Parameters<typeof mammoth.convertToHtml>[1]) => (
      mammoth.convertToHtml({ buffer: Buffer.from(input.arrayBuffer) }, options)
    ),
  } }
})

function docx(text = '门店月报') {
  return zipSync({
    '[Content_Types].xml': strToU8('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
    '_rels/.rels': strToU8('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
    'word/document.xml': strToU8(`<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>${text}</w:t></w:r></w:p></w:body></w:document>`),
  }).buffer
}

function workbook(rows: (string | number)[][]) {
  const book = XLSX.utils.book_new()
  const sheet = XLSX.utils.aoa_to_sheet(rows)
  XLSX.utils.book_append_sheet(book, sheet, '门店营收')
  return { book, sheet }
}

const workbookBytes = (book: XLSX.WorkBook): ArrayBuffer => XLSX.write(book, { type: 'array', bookType: 'xlsx' })

describe('文档内容解析', () => {
  it('只读取首工作表与保存的值，不计算公式或跟随超链接', async () => {
    const { book, sheet } = workbook([['月份', '营收'], ['九月', 42]])
    sheet.B2.f = 'WEBSERVICE("https://example.invalid/private")'
    sheet.A2.l = { Target: 'javascript:alert(1)' }
    XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([['不应显示的第二张表']]), '成本')
    const value = await parseOfficeDocument(workbookBytes(book), 'xlsx')
    expect(value).toMatchObject({ kind: 'xlsx', sheet: '门店营收', rows: [['月份', '营收'], ['九月', '42']], columns: ['A', 'B'], truncated: false })
    expect(JSON.stringify(value)).not.toContain('WEBSERVICE')
    expect(JSON.stringify(value)).not.toContain('javascript:')
  })
  it('在解压前拒绝超过实际输入或声明解压上限的文件', async () => {
    await expect(parseOfficeDocument(new ArrayBuffer(DOCUMENT_PREVIEW_BYTES + 1), 'docx')).rejects.toMatchObject({ code: 'size' })
    const bytes = docx()
    const view = new DataView(bytes)
    for (let offset = 0; offset < view.byteLength - 46; offset += 1) {
      if (view.getUint32(offset, true) === 0x02014b50) {
        view.setUint32(offset + 24, 33 * 1024 * 1024, true)
        break
      }
    }
    await expect(parseOfficeDocument(bytes, 'docx')).rejects.toMatchObject({ code: 'size' })
  })
  it('解包前拒绝未压缩条目互相矛盾的长度，正常未压缩文档仍可读取', async () => {
    const entries = unzipSync(new Uint8Array(docx()))
    const bytes = zipSync(entries, { level: 0 }).buffer
    await expect(parseOfficeDocument(bytes, 'docx')).resolves.toEqual({ kind: 'docx', html: '<p>门店月报</p>' })
    const view = new DataView(bytes)
    for (let offset = 0; offset < view.byteLength - 46; offset += 1) {
      if (view.getUint32(offset, true) === 0x02014b50) {
        view.setUint32(offset + 24, 0, true)
        break
      }
    }
    await expect(parseOfficeDocument(bytes, 'docx')).rejects.toMatchObject({ code: 'invalid' })
    const { book } = workbook([['门店', '营收'], ['虹桥店', 57600]])
    await expect(parseOfficeDocument(workbookBytes(book), 'xlsx')).resolves.toMatchObject({
      kind: 'xlsx', rows: [['门店', '营收'], ['虹桥店', '57600']],
    })
  })
})
