import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import license from 'rollup-plugin-license'

export default defineConfig(() => {
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
