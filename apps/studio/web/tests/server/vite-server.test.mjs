import assert from 'node:assert/strict'
import { once } from 'node:events'
import net from 'node:net'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { withViteTestServer } from './http-servers.mjs'

test('临时前端服务在用例异常后关闭监听和已有连接', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-http-cleanup-'))
  const failure = new Error('受控用例失败')
  let server, client, closed
  try {
    await assert.rejects(withViteTestServer({
      configFile: false, root, appType: 'custom', logLevel: 'silent',
    }, async (frontend) => {
      server = frontend.server
      const accepted = once(server, 'connection')
      client = net.createConnection({ host: '127.0.0.1', port: Number(new URL(frontend.origin).port) })
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
