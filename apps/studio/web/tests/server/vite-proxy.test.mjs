import assert from 'node:assert/strict'
import { EventEmitter, once } from 'node:events'
import http from 'node:http'
import net from 'node:net'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { closeHttpServers, listenOnLoopback, withViteTestServer } from './http-servers.mjs'

test('Vite forwards normal SSE and closes interrupted upstream responses', async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-proxy-'))
  const proxiedResponses = new EventEmitter()
  const heldResponses = new Map()
  const frame = 'data: {"started":true}\n\n'
  const upstream = http.createServer((request, response) => {
    response.writeHead(200, { 'content-type': 'text/event-stream', 'x-test-upstream': 'owned' })
    response.flushHeaders()
    if (request.url === '/api/normal') {
      response.end('data: {"ok":true}\n\n')
      return
    }
    heldResponses.set(request.url, response)
    if (request.url === '/api/after-frame') response.write(frame)
  })
  try {
    const target = await listenOnLoopback(upstream)
    await withViteTestServer({
      configFile: fileURLToPath(new URL('../../vite.config.ts', import.meta.url)),
      root,
      logLevel: 'silent',
      optimizeDeps: { noDiscovery: true },
      server: { proxy: { '/api': { target } } },
      plugins: [{
        name: 'observe-test-proxy-response',
        configResolved(config) {
          // 保留真实代理配置，仅通过公开事件确定上游响应已到达代理
          const options = config.server.proxy['/api']
          const configure = options.configure
          options.configure = (proxy, options) => {
            configure?.(proxy, options)
            proxy.on('proxyRes', (upstream, request, downstream) => {
              proxiedResponses.emit(request.url, { upstream, downstream })
            })
          }
        },
      }],
    }, async ({ origin }) => {
      await t.test('normal EOF is preserved for loopback Host names', async () => {
        for (const host of ['127.0.0.1', 'localhost']) {
          // Host 校验始终连接本测试的 IPv4 服务，不依赖本机 DNS 顺序
          const response = await fetch(`${origin}/api/normal`, {
            headers: { Host: `${host}:${new URL(origin).port}` },
            signal: t.signal,
          })
          assert.equal(response.status, 200)
          assert.equal(response.headers.get('x-test-upstream'), 'owned')
          assert.equal(await response.text(), 'data: {"ok":true}\n\n')
        }
      })
      for (const endpoint of ['before-frame', 'after-frame']) {
        await t.test(endpoint, async (context) => {
          const route = `/api/${endpoint}`
          const arrived = once(proxiedResponses, route, { signal: context.signal })
          const request = fetch(`${origin}${route}`, { signal: context.signal })
          const [proxied] = await arrived
          const aborted = once(proxied.upstream, 'aborted', { signal: context.signal })
          if (endpoint === 'before-frame') {
            const rejected = assert.rejects(request.then(response => response.text()), { name: 'TypeError' })
            heldResponses.get(route).destroy()
            await aborted
            assert.equal(proxied.downstream.destroyed, true)
            await rejected
          } else {
            const response = await request
            assert.equal(response.status, 200)
            const reader = response.body.getReader()
            try {
              const decoder = new TextDecoder()
              let received = ''
              while (received.length < frame.length) {
                const chunk = await reader.read()
                assert.equal(chunk.done, false)
                received += decoder.decode(chunk.value, { stream: true })
              }
              assert.equal(received, frame)
              // 已消费首帧后才中断上游，不依赖传输速度或固定等待
              heldResponses.get(route).destroy()
              await aborted
              assert.equal(proxied.downstream.destroyed, true)
              // 关闭定界的响应允许 EOF，也允许连接中断；不得出现额外内容
              const terminal = await reader.read().catch(error => {
                assert.equal(error.name, 'TypeError')
                return { done: true }
              })
              assert.equal(terminal.done, true)
            } finally {
              reader.releaseLock()
            }
          }
          heldResponses.delete(route)
        })
      }
    })
  } finally {
    try {
      await closeHttpServers([upstream])
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  }
})

test('临时代理在用例异常后关闭监听和已有连接', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-proxy-cleanup-'))
  const failure = new Error('受控用例失败')
  let server, client, closed
  try {
    await assert.rejects(withViteTestServer({
      configFile: false, root, appType: 'custom', logLevel: 'silent',
    }, async (proxy) => {
      server = proxy.server
      const accepted = once(server, 'connection')
      client = net.createConnection({ host: '127.0.0.1', port: Number(new URL(proxy.origin).port) })
      await Promise.all([once(client, 'connect'), accepted])
      closed = once(client, 'close')
      throw failure
    }), error => error === failure)
    await closed
    assert.equal(server.listening, false)
    assert.equal(server.address(), null)
    assert.equal(client.destroyed, true)
  } finally {
    client?.destroy()
    await rm(root, { recursive: true, force: true })
  }
})
