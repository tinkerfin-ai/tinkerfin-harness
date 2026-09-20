import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import license from 'rollup-plugin-license'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react()],
    worker: {
      plugins: () => {
        let notices = ''
        return [
          license({ thirdParty: { output(dependencies) {
            notices = dependencies.map(dependency => {
              if (!dependency.name || !dependency.version || !dependency.license) {
                throw new Error(`Missing worker dependency license: ${dependency.name}`)
              }
              return `## ${dependency.name} - ${dependency.version} (${dependency.license})\n\n${dependency.text()}`
            }).join('\n\n')
          } } }),
          {
            name: 'document-preview-licenses',
            generateBundle() {
              this.emitFile({ type: 'asset', fileName: 'document-preview-licenses.md', source: notices })
            },
          },
        ]
      },
    },
    build: {
      license: {
        fileName: 'third-party-licenses.md',
      },
    },
    server: {
      host: '127.0.0.1',
      port: 5190,
      strictPort: true,
      proxy: {
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8090',
          changeOrigin: true,
          configure(proxy) {
            // 上游响应中途关闭时结束下游，避免浏览器一直等不到响应头或 EOF
            proxy.on('proxyRes', (upstream, _request, downstream) => {
              const fail = () => {
                if (!downstream.writableFinished && !downstream.destroyed) downstream.destroy()
              }
              upstream.once('aborted', fail)
              upstream.once('error', fail)
              upstream.once('close', () => {
                if (!upstream.complete) fail()
              })
            })
          },
        },
      },
    },
    test: {
      include: ['src/**/*.test.{ts,tsx}'],
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.ts',
      css: true,
      // 限制同时运行的测试文件，控制共享开发机上的 DOM 环境数量
      maxWorkers: 2,
    },
  }
})
