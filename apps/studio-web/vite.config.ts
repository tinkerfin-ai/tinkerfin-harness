import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import license from 'rollup-plugin-license'
import { loadEnv } from 'vite'
import { connectionMetadata } from './tools/connectionMetadata'
import { LOCAL_STUDIO_API_ADDRESS } from './src/api/shared/serviceAddress'

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, process.cwd(), 'VITE_')
  return {
    plugins: [react(), connectionMetadata(environment.VITE_STUDIO_API_ADDRESS || LOCAL_STUDIO_API_ADDRESS)],
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
