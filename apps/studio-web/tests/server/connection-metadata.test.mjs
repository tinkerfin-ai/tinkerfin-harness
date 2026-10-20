import assert from 'node:assert/strict'
import { mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test } from 'node:test'
import { build, createServer } from 'vite'

const configFile = resolve('vite.config.ts')

test('桌面连接描述在开发服务和生产构建使用相同 API 地址', async () => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'tinkerfin-connection-')))
  const previous = process.env.VITE_STUDIO_API_ADDRESS
  process.env.VITE_STUDIO_API_ADDRESS = 'https://api.example.com/studio/'
  let server
  try {
    await writeFile(join(directory, 'index.html'), '<title>Studio</title>')
    server = await createServer({
      configFile, root: directory, base: '/studio/', logLevel: 'silent',
      server: { host: '127.0.0.1', port: 0 },
    })
    await server.listen()
    const address = server.httpServer?.address()
    assert.ok(address && typeof address !== 'string')
    const url = `http://127.0.0.1:${address.port}/studio/tinkerfin-connection.json`
    const response = await fetch(url)
    assert.equal(response.headers.get('cache-control'), 'no-store')
    const description = await response.json()
    assert.deepEqual(description, { api_url: 'https://api.example.com/studio' })
    assert.equal((await fetch(url, { method: 'POST' })).status, 405)
    assert.equal(await (await fetch(url, { method: 'HEAD' })).text(), '')

    const result = await build({
      configFile, root: directory, base: '/studio/', logLevel: 'silent',
      build: { write: false },
    })
    assert.ok(!Array.isArray(result) && 'output' in result)
    const metadata = result.output.find(item => item.fileName === 'tinkerfin-connection.json')
    assert.ok(metadata && metadata.type === 'asset')
    assert.deepEqual(JSON.parse(String(metadata.source)), description)
  } finally {
    await server?.close()
    if (previous === undefined) delete process.env.VITE_STUDIO_API_ADDRESS
    else process.env.VITE_STUDIO_API_ADDRESS = previous
    await rm(directory, { recursive: true, force: true })
  }
})
