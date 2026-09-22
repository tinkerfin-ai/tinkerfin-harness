import assert from 'node:assert/strict'
import { once } from 'node:events'
import http from 'node:http'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { build, createServer, preview } from 'vite'

/** 为测试自有服务分配独立的 IPv4 回环端口 */
export async function listenOnLoopback(server) {
  const listening = once(server, 'listening')
  server.listen(0, '127.0.0.1')
  await listening
  return `http://127.0.0.1:${server.address().port}`
}

/** 停止接收请求并关闭现有连接，等待所有自有服务完成清理 */
export async function closeHttpServers(servers) {
  const results = await Promise.allSettled(servers.map(async server => {
    await new Promise((resolve, reject) => {
      if (!server.listening) {
        server.closeAllConnections()
        resolve()
        return
      }
      server.close(error => error ? reject(error) : resolve())
      server.closeAllConnections()
    })
    assert.equal(server.address(), null)
    const connections = await new Promise((resolve, reject) => {
      server.getConnections((error, count) => error ? reject(error) : resolve(count))
    })
    assert.equal(connections, 0)
  }))
  const errors = results.filter(result => result.status === 'rejected').map(result => result.reason)
  if (errors.length) throw new AggregateError(errors, '测试 HTTP 服务清理失败')
}

/** 在独立端口承载真实 Vite 中间件，并在用例结束或失败后释放两者 */
export async function withViteTestServer(config, run) {
  const server = http.createServer()
  let vite, failure
  try {
    vite = await createServer({
      ...config,
      server: {
        ...config.server,
        middlewareMode: { server },
        hmr: false,
        ws: false,
        watch: null,
      },
    })
    server.on('request', vite.middlewares)
    const origin = await listenOnLoopback(server)
    return await run({ vite, server, origin })
  } catch (error) {
    failure = error
    throw error
  } finally {
    const results = await Promise.allSettled([closeHttpServers([server]), vite?.close()])
    const errors = results.filter(result => result.status === 'rejected').map(result => result.reason)
    if (errors.length) {
      throw new AggregateError(failure === undefined ? errors : [failure, ...errors], '测试前端服务清理失败')
    }
  }
}

/** 使用本轮独占的构建目录预览，避免其他构建改变正在验证的页面 */
export async function withBuiltPreview(config, run) {
  const directory = await mkdtemp(path.join(tmpdir(), 'studio-browser-build-'))
  let server, failure
  try {
    const buildOptions = { ...config.build, outDir: directory, emptyOutDir: true, watch: null }
    await build({ ...config, build: buildOptions })
    server = await preview({
      ...config,
      build: buildOptions,
      plugins: [...(config.plugins ?? []), {
        name: 'own-browser-preview',
        // 监听端口失败时 preview 不会返回，提前保存服务以释放其信号监听器
        configurePreviewServer(previewServer) { server = previewServer },
      }],
    })
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`
    return await run({ server: server.httpServer, origin, directory })
  } catch (error) {
    failure = error
    throw error
  } finally {
    const errors = []
    try {
      if (server) await server.close()
    } catch (error) {
      errors.push(error)
    }
    try {
      await rm(directory, { recursive: true, force: true })
    } catch (error) {
      errors.push(error)
    }
    if (errors.length) {
      throw new AggregateError(failure === undefined ? errors : [failure, ...errors], '浏览器测试预览清理失败')
    }
  }
}
