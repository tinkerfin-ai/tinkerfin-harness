import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: true,
  use: { baseURL: 'http://127.0.0.1:8770', viewport: { width: 650, height: 360 }, contextOptions: { reducedMotion: 'reduce' } },
  webServer: { command: 'pnpm exec vite preview --host 127.0.0.1 --port 8770 --strictPort', url: 'http://127.0.0.1:8770', reuseExistingServer: false },
  projects: [{ name: 'webkit', use: { browserName: 'webkit' } }],
})
