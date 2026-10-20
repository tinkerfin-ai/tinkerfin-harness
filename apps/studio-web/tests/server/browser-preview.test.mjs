import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import http from 'node:http'
import { test } from 'node:test'
import { closeHttpServers, listenOnLoopback, withBuiltPreview } from './http-servers.mjs'

for (const fail of [false, true]) {
  test(`浏览器预览不受共享构建重写影响且会清理（异常：${fail}）`, async () => {
    const root = await realpath(await mkdtemp(path.join(tmpdir(), 'studio-preview-source-')))
    const shared = path.join(root, 'dist')
    const failure = new Error('受控用例失败')
    const signalListeners = process.listenerCount('SIGTERM')
    let owned
    try {
      await writeFile(path.join(root, 'index.html'), '<html data-theme="dark"><body>本轮页面</body></html>')
      const running = withBuiltPreview({
        root, configFile: false, logLevel: 'silent',
        preview: { host: '127.0.0.1', port: 0, strictPort: true },
      }, async preview => {
        owned = preview
        assert.notEqual(preview.directory, shared)
        const first = await fetch(preview.origin)
        assert.equal(first.status, 200)
        const original = await first.text()
        assert.match(original, /data-theme="dark"/)

        await writeFile(path.join(root, 'index.html'), '<html><body>另一次构建</body></html>')
        await mkdir(shared)
        await writeFile(path.join(shared, 'index.html'), '<html><body>共享产物</body></html>')
        await rm(shared, { recursive: true })

        const reloaded = await fetch(preview.origin)
        assert.equal(reloaded.status, 200)
        assert.equal(await reloaded.text(), original)
        if (fail) throw failure
      })
      if (fail) await assert.rejects(running, error => error === failure)
      else await running
      assert.equal(owned.server.address(), null)
      await assert.rejects(access(owned.directory), { code: 'ENOENT' })
      assert.equal(process.listenerCount('SIGTERM'), signalListeners)
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })
}

test('预览端口被占用时清理本轮构建和监听器，保留原有服务', async () => {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), 'studio-preview-source-')))
  const occupied = http.createServer((_request, response) => response.end('原有服务'))
  const listeners = process.listenerCount('SIGTERM')
  let directory
  try {
    const origin = await listenOnLoopback(occupied)
    await writeFile(path.join(root, 'index.html'), '<html><body>本轮页面</body></html>')
    await assert.rejects(withBuiltPreview({
      root, configFile: false, logLevel: 'silent',
      preview: { host: '127.0.0.1', port: occupied.address().port, strictPort: true },
      plugins: [{
        name: 'observe-owned-build',
        configResolved(config) { directory = config.build.outDir },
      }],
    }, () => assert.fail('占用端口时不得开始用例')), /already in use/)
    await assert.rejects(access(directory), { code: 'ENOENT' })
    assert.equal(process.listenerCount('SIGTERM'), listeners)
    assert.equal(await (await fetch(origin)).text(), '原有服务')
  } finally {
    await closeHttpServers([occupied])
    await rm(root, { recursive: true, force: true })
  }
})
