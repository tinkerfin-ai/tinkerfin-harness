import { Blob, File } from 'node:buffer'
import { transferableAbortController } from 'node:util'
import '@testing-library/jest-dom/vitest'
import { beforeEach, vi } from 'vitest'

// 页面事件仍由 jsdom 处理，保留其原生取消构造器供 DOM 回归使用
export const DomFile = globalThis.File
export const DomAbortController = globalThis.AbortController
// 请求使用 Node 原生 fetch 家族，文件和取消信号与 Request 保持同源
const requestAbortController = transferableAbortController()
Object.defineProperties(globalThis, {
  Blob: {configurable: true, writable: true, value: Blob},
  File: {configurable: true, writable: true, value: File},
  AbortController: {configurable: true, writable: true, value: requestAbortController.constructor},
  AbortSignal: {configurable: true, writable: true, value: requestAbortController.signal.constructor},
})

const memory = new Map<string, string>()
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => memory.get(key) ?? null,
    setItem: (key: string, value: string) => memory.set(key, String(value)),
    removeItem: (key: string) => memory.delete(key),
    clear: () => memory.clear(),
    key: (index: number) => [...memory.keys()][index] ?? null,
    get length() { return memory.size },
  },
})

Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: (query: string) => {
    const minWidth = query.match(/min-width:\s*(\d+)px/)?.[1]
    const maxWidth = query.match(/max-width:\s*(\d+)px/)?.[1]
    const matches = query.includes('prefers-')
      ? false
      : (!minWidth || window.innerWidth >= Number(minWidth))
        && (!maxWidth || window.innerWidth <= Number(maxWidth))
    return {
    matches,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  }
  },
})

Object.defineProperty(globalThis, 'ResizeObserver', {
  configurable: true,
  value: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
})

beforeEach(() => {
  window.localStorage.clear()
})
