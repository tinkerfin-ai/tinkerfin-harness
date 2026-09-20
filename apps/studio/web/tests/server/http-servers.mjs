import assert from 'node:assert/strict'
import { once } from 'node:events'
import http from 'node:http'
import { createServer } from 'vite'

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
      throw new AggregateError(failure === undefined ? errors : [failure, ...errors], '测试代理清理失败')
    }
  }
}
