import assert from 'node:assert/strict'
import http from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'
import { closeHttpServers, listenOnLoopback, withViteTestServer } from './http-servers.mjs'

const webRoot = fileURLToPath(new URL('../../', import.meta.url))

test('真实跨域表单直传保留文件与取消，业务凭据仅发送给后端', { timeout: 30_000 }, async () => {
  const cache = await mkdtemp(path.join(tmpdir(), 'studio-upload-'))
  const received = []
  const apiRequests = []
  let held
  let entered
  const pendingUpload = new Promise(resolve => { entered = resolve })
  const objectServer = http.createServer(async (request, response) => {
    response.setHeader('Access-Control-Allow-Origin', '*')
    if (request.method === 'OPTIONS') {
      response.setHeader('Access-Control-Allow-Methods', 'POST')
      response.setHeader('Access-Control-Allow-Headers', 'content-type')
      response.end(); return
    }
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const form = await new Response(Buffer.concat(chunks), { headers: { 'content-type': request.headers['content-type'] } }).formData()
    const file = form.get('file')
    received.push({ name: file.name, bytes: Buffer.from(await file.arrayBuffer()), authorization: request.headers.authorization, cookie: request.headers.cookie, policy: form.get('policy') })
    if (file.name === 'cancel.pdf') { held = response; entered(); return }
    response.writeHead(204).end()
  })
  const permits = new Map()
  const upstream = http.createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    apiRequests.push({ url: request.url, authorization: request.headers.authorization, bytes: Buffer.concat(chunks) })
    let data
    if (request.url === '/api/attachments/uploads') {
      const body = JSON.parse(Buffer.concat(chunks))
      const id = String(permits.size)
      permits.set(id, body)
      data = { attachment_id: id, url: `http://127.0.0.1:${objectServer.address().port}/bucket`, fields: { key: id, policy: 'signed-policy' }, expires_in: 600 }
    } else {
      const id = request.url.split('/')[3]
      const metadata = permits.get(id)
      data = { id, name: metadata.name, mime_type: 'application/pdf', size_bytes: metadata.size_bytes }
    }
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ code: 0, message: 'success', data }))
  })
  try {
    await listenOnLoopback(objectServer)
    const target = await listenOnLoopback(upstream)
    await withViteTestServer({ root: webRoot, configFile: false, cacheDir: cache,
      appType: 'custom', server: { proxy: { '/api': { target } } },
    }, async ({ vite, origin }) => {
      vite.middlewares.use('/__upload_test__', (_, res) => { res.setHeader('Content-Type', 'text/html'); res.end('<html><body>Upload test</body></html>') })
      const browser = await chromium.launch()
      try {
        const page = await browser.newPage()
        await page.goto(`${origin}/__upload_test__`)
        await page.evaluate(() => localStorage.setItem('tinkerfin.auth.session', JSON.stringify({
          token: 'isolated-upload-token', tokenType: 'Bearer', expiresAt: '2099-01-01T00:00:00.000Z',
          user: { user_id: 1, username: 'test', display_name: '附件测试', avatar_url: null, roles: [], disabled: false },
        })))
        const result = await page.evaluate(async () => {
          const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
          const progress = []
          const data = Uint8Array.from([37, 80, 68, 70, 0, 255])
          const attachment = await uploadAttachment(new File([data], '中文 附件.pdf'), new AbortController().signal, p => progress.push(p))
          return { attachment, progress }
        })
        assert.equal(result.attachment.name, '中文 附件.pdf')
        assert.equal(result.progress.at(-1), 100)
        assert.deepEqual(received[0].bytes, Buffer.from([37, 80, 68, 70, 0, 255]))
        assert.equal(received[0].policy, 'signed-policy')
        assert.equal(received[0].authorization, undefined)
        assert.equal(received[0].cookie, undefined)
        assert.ok(apiRequests.every(r => r.authorization === 'Bearer isolated-upload-token'))
        assert.equal(apiRequests.length, 2)
        assert.equal(JSON.parse(apiRequests[0].bytes).size_bytes, 6)
        await page.evaluate(async () => {
          const { uploadAttachment } = await import('/src/features/conversation/attachments/client.ts')
          const controller = new AbortController()
          globalThis.uploadTest = { controller, completion: uploadAttachment(new File(['pdf'], 'cancel.pdf'), controller.signal, () => {}).then(() => 'success', e => e.code) }
        })
        await pendingUpload
        await page.evaluate(() => globalThis.uploadTest.controller.abort())
        assert.equal(await page.evaluate(() => globalThis.uploadTest.completion), 'ERR_CANCELED')
        assert.equal(apiRequests.filter(r => r.url.endsWith('/complete')).length, 1)
      } finally {
        await browser.close()
      }
    })
  } finally {
    held?.destroy()
    try {
      await closeHttpServers([upstream, objectServer])
    } finally {
      await rm(cache, { recursive: true, force: true })
    }
  }
})
