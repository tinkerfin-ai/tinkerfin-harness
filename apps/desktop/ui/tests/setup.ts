import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(cleanup)
window.matchMedia = vi.fn().mockImplementation(query => ({ matches: query.includes(': reduce)'), media: query, onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return true } }))
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
