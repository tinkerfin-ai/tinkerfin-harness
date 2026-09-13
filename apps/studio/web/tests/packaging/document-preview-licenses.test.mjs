import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { runInNewContext } from 'node:vm'
import test from 'node:test'

const require = createRequire(import.meta.url)

// 校验发布内容中的许可证，不把源码或用户文档文案当作测试契约
const packageMetadata = async (name, resolver = require) => {
  let directory = dirname(resolver.resolve(name))
  while (dirname(directory) !== directory) {
    if ((await readdir(directory)).includes('package.json')) {
      const metadata = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'))
      if (metadata.name === name) return metadata
    }
    directory = dirname(directory)
  }
  throw new Error(`Cannot locate installed package metadata: ${name}`)
}

const heading = ({ name, version, license }) => `## ${name} - ${version} (${license})`


test('主页面与文档解析线程均包含实际依赖的许可证材料', async () => {
  const main = await readFile('dist/third-party-licenses.md', 'utf8')
  const worker = await readFile('dist/document-preview-licenses.md', 'utf8')
  const purifier = require('dompurify').version
  assert.ok(main.includes(`## dompurify - ${purifier} `))
  for (const name of ['mammoth', 'xlsx', 'fflate']) assert.ok(worker.includes(heading(await packageMetadata(name))), name)
  const mammothRequire = createRequire(require.resolve('mammoth'))
  for (const name of ['@xmldom/xmldom', 'jszip']) {
    const { version, license } = await packageMetadata(name, mammothRequire)
    assert.ok(worker.includes(`## ${name} - ${version} (${license})`), name)
  }
  for (const section of worker.split(/^## /m).filter(Boolean)) {
    assert.ok(section.includes('License:') && section.includes('Version:'), section.split('\n')[0])
  }
  const files = await readdir('dist/assets')
  assert.ok(files.some(name => /^documentPreview\.worker-.*\.js$/.test(name)))
})


// 执行真实构建后的浏览器 Worker，核对它不依赖 Node 的文件输入路径
// 此验证不代替浏览器 UI、PDF 插件与鉴权请求的端到端检查
for (const format of ['docx', 'xlsx']) {
  test(`打包后的 ${format} 线程可从浏览器 ArrayBuffer 读取实际文档`, async () => {
    const { zipSync, strToU8 } = require('fflate')
    const XLSX = require('xlsx')
    let bytes
    if (format === 'docx') {
      bytes = zipSync({
        '[Content_Types].xml': strToU8('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        '_rels/.rels': strToU8('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
        'word/document.xml': strToU8('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>九月营收</w:t></w:r></w:p></w:body></w:document>'),
      }).buffer
    } else {
      const book = XLSX.utils.book_new()
      XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([['营收', 42]]), '月报')
      bytes = XLSX.write(book, { type: 'array', bookType: 'xlsx' })
    }
    const file = (await readdir('dist/assets')).find(name => /^documentPreview\.worker-.*\.js$/.test(name))
    assert.ok(file)
    const code = await readFile(join('dist/assets', file), 'utf8')
    const result = await new Promise((resolve, reject) => {
      const worker = {
        postMessage: resolve, ArrayBuffer, Uint8Array, TextEncoder, TextDecoder, setTimeout, clearTimeout,
        importScripts() { throw new Error('Module workers cannot import scripts synchronously') },
      }
      worker.self = worker
      try {
        runInNewContext(code, worker)
        worker.onmessage({ data: { bytes, format } }).catch(reject)
      } catch (error) { reject(error) }
    })
    assert.ok(result.content, JSON.stringify(result))
    if (format === 'docx') assert.equal(result.content?.html, '<p>九月营收</p>')
    else assert.deepEqual(JSON.parse(JSON.stringify(result.content?.rows)), [['营收', '42']])
  })
}
