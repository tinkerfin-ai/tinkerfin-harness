import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import license from 'rollup-plugin-license'

const directory = fileURLToPath(new URL('.', import.meta.url))
const fontNotices = ['inter', 'jetbrains-mono', 'noto-sans-sc'].map(name =>
  `@fontsource-variable/${name}\n${readFileSync(resolve(directory, `node_modules/@fontsource-variable/${name}/LICENSE`), 'utf8')}`,
).join('\n\n')

export default defineConfig({
  base: './',
  plugins: [react(), license({ thirdParty: { output: {
    file: resolve(directory, 'dist/THIRD_PARTY_NOTICES.txt'),
    template: dependencies => dependencies.map(dependency => `${dependency.name} ${dependency.version}\n${dependency.licenseText}`).join('\n\n') + '\n\n' + fontNotices,
  } } })],
  build: { target: 'safari18', assetsInlineLimit: 0 },
  test: { environment: 'jsdom', setupFiles: ['./tests/setup.ts'], include: ['tests/**/*.test.{ts,tsx}'] },
})
