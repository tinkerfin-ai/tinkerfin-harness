import assert from 'node:assert/strict'
import http from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

test('Vite forwards normal SSE and closes interrupted upstream responses', async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-proxy-'))
  const upstream = http.createServer((request, response) => {
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    response.flushHeaders()
    if (request.url === '/api/normal') {
      response.end('data: {"ok":true}\n\n')
      return
    }
    if (request.url === '/api/after-frame') response.write('data: {"started":true}\n\n')
    setTimeout(() => response.destroy(), 20)
  })
  await new Promise((resolve) => upstream.listen(0, '127.0.0.1', resolve))
  const target = `http://127.0.0.1:${upstream.address().port}`
  let proxy
  try {
    proxy = await createServer({
      configFile: fileURLToPath(new URL('../../vite.config.ts', import.meta.url)),
      root,
      logLevel: 'silent',
      optimizeDeps: { noDiscovery: true },
      server: { port: 0, hmr: false, watch: null, proxy: { '/api': { target } } },
    })
    await proxy.listen()
    const base = `http://127.0.0.1:${proxy.httpServer.address().port}`
    await t.test('normal EOF is preserved', async () => {
      for (const host of ['127.0.0.1', 'localhost']) {
        const response = await fetch(`http://${host}:${proxy.httpServer.address().port}/api/normal`)
        assert.equal(response.status, 200)
        assert.equal(await response.text(), 'data: {"ok":true}\n\n')
      }
    })
    for (const endpoint of ['before-frame', 'after-frame']) {
      await t.test(endpoint, async () => {
        const deadline = new AbortController()
        const timer = setTimeout(() => deadline.abort(), 1500)
        try {
          const readResponse = async () => {
            const response = await fetch(`${base}/api/${endpoint}`, { signal: deadline.signal })
            return response.text()
          }
          if (endpoint === 'before-frame') await assert.rejects(readResponse)
          else {
            // 关闭定界的 HTTP 响应可以表现为 EOF；消费端仍须检查协议终态
            const result = await readResponse().catch(() => null)
            if (result !== null) assert.equal(result, 'data: {"started":true}\n\n')
          }
          assert.equal(deadline.signal.aborted, false, 'proxy must close before the test deadline')
        } finally {
          clearTimeout(timer)
        }
      })
    }
  } finally {
    proxy?.httpServer?.closeAllConnections()
    if (proxy) await proxy.close()
    upstream.closeAllConnections()
    await new Promise((resolve) => upstream.close(resolve))
    await rm(root, { recursive: true, force: true })
  }
})
