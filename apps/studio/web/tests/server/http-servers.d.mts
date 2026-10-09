import type { Server } from 'node:http'
import type { InlineConfig, ViteDevServer } from 'vite'

export function listenOnLoopback(server: Server): Promise<string>
export function closeHttpServers(servers: Server[]): Promise<void>
export function withViteTestServer<Result>(
  config: InlineConfig,
  run: (context: { vite: ViteDevServer; server: Server; origin: string }) => Promise<Result> | Result,
): Promise<Result>
export function withBuiltPreview<Result>(
  config: InlineConfig,
  run: (context: { server: Server; origin: string; directory: string }) => Promise<Result> | Result,
): Promise<Result>
