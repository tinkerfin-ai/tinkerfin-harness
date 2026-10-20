import type { Plugin } from 'vite'
import { parseServiceAddress } from '../src/api/shared/serviceAddress'

/** 桌面客户端通过 Studio 网址发现同一次构建使用的 API 地址 */
export function connectionMetadata(apiAddress: string): Plugin {
  const filename = 'tinkerfin-connection.json'
  const source = JSON.stringify({ api_url: parseServiceAddress(apiAddress) }) + '\n'
  return {
    name: 'tinkerfin-connection',
    configureServer(server) {
      const path = `${server.config.base}${filename}`
      server.middlewares.use((request, response, next) => {
        if (request.url?.split('?', 1)[0] !== path) return next()
        if (request.method !== 'GET' && request.method !== 'HEAD') {
          response.statusCode = 405
          response.setHeader('Allow', 'GET, HEAD')
          response.end()
          return
        }
        response.setHeader('Content-Type', 'application/json; charset=utf-8')
        response.setHeader('Cache-Control', 'no-store')
        response.end(request.method === 'HEAD' ? undefined : source)
      })
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: filename, source })
    },
  }
}
