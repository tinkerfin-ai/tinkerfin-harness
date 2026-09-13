import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  normalizeAppLocation,
  readPageFromLocation,
  readThreadFromLocation,
  writeWorkspaceToLocation,
} from './threadRoute'

describe('threadRoute', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  it('reads the thread id from ?thread=', () => {
    window.history.replaceState(null, '', '/?thread=abc-123')
    expect(readThreadFromLocation()).toBe('abc-123')
  })

  it('returns an empty string when ?thread is absent', () => {
    window.history.replaceState(null, '', '/')
    expect(readThreadFromLocation()).toBe('')
  })

  it('writes the thread id into the URL', () => {
    writeWorkspaceToLocation('conversation', 'thread-xyz')
    expect(window.location.search).toContain('thread=thread-xyz')
    expect(readThreadFromLocation()).toBe('thread-xyz')
  })

  it('removes the param when given an empty thread id', () => {
    window.history.replaceState(null, '', '/?thread=stale')
    writeWorkspaceToLocation('conversation', '')
    expect(window.location.search).toBe('')
    expect(readThreadFromLocation()).toBe('')
  })

  it('adds a browser history entry when the URL changes', () => {
    const pushState = vi.spyOn(window.history, 'pushState')
    writeWorkspaceToLocation('conversation', 'next-thread')
    expect(pushState).toHaveBeenCalledWith(null, '', '/?thread=next-thread')
  })

  it('does not write history when the URL already matches', () => {
    window.history.replaceState(null, '', '/?thread=already')
    const pushState = vi.spyOn(window.history, 'pushState')
    writeWorkspaceToLocation('conversation', 'already')
    expect(pushState).not.toHaveBeenCalled()
  })

  it('can replace the current entry for auth cleanup', () => {
    window.history.replaceState(null, '', '/?thread=signed-in')
    const replaceState = vi.spyOn(window.history, 'replaceState')
    writeWorkspaceToLocation('conversation', '', { history: 'replace' })
    expect(replaceState).toHaveBeenCalledWith(null, '', '/')
  })

  it('restores the last valid thread URL when an unsupported path is opened', () => {
    window.history.replaceState(null, '', '/?thread=remembered-thread')
    normalizeAppLocation()
    window.history.replaceState(null, '', '/sssssssssssssssd#top')

    normalizeAppLocation()

    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toBe('?thread=remembered-thread')
    expect(window.location.hash).toBe('')
  })

  it('falls back to the app root when no valid location was recorded', () => {
    window.history.replaceState(null, '', '/not-a-route?thread=unknown#top')

    normalizeAppLocation()

    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toBe('')
    expect(window.location.hash).toBe('')
  })

  it('keeps conversation scroll state while correcting an unsupported path', () => {
    window.history.replaceState(null, '', '/?thread=scroll-thread')
    normalizeAppLocation()
    window.sessionStorage.setItem('tinkerfin:conversation-scroll:scroll-thread', '240')
    window.history.replaceState(null, '', '/wrong-address')

    normalizeAppLocation()

    expect(readThreadFromLocation()).toBe('scroll-thread')
    expect(window.sessionStorage.getItem('tinkerfin:conversation-scroll:scroll-thread')).toBe('240')
  })
})

describe('页面与会话独立恢复', () => {
  afterEach(() => { window.history.replaceState(null, '', '/'); window.sessionStorage.clear() })
  it('自动化地址不携带后台会话，返回对话时恢复指定会话并保留其他 URL 信息', () => {
    window.history.replaceState(null, '', '/?thread=existing&filter=a#top')
    writeWorkspaceToLocation('automation', 'existing')
    expect(readPageFromLocation()).toBe('automation')
    expect(window.location.search).toBe('?filter=a&page=automation')
    expect(readThreadFromLocation()).toBe('')
    writeWorkspaceToLocation('automation', 'next')
    expect(window.location.search).toBe('?filter=a&page=automation')
    writeWorkspaceToLocation('conversation', 'next')
    expect(window.location.search).toBe('?filter=a&thread=next')
    expect(window.location.hash).toBe('#top')
  })
  it('自动化页不从无关 thread 参数恢复会话', () => {
    window.history.replaceState(null, '', '/?page=automation&thread=irrelevant')
    expect(readThreadFromLocation()).toBe('')
  })
  it('自动化刷新、重复写入和未知路径恢复不依赖会话，返回草稿清空页面参数', () => {
    window.history.replaceState(null, '', '/?thread=old')
    writeWorkspaceToLocation('automation', 'old')
    const replace = vi.spyOn(window.history, 'replaceState')
    writeWorkspaceToLocation('automation', 'old')
    expect(replace).not.toHaveBeenCalled()
    replace.mockRestore()
    window.history.replaceState(null, '', '/unknown')
    normalizeAppLocation()
    expect(window.location.search).toBe('?page=automation')
    expect(readThreadFromLocation()).toBe('')
    writeWorkspaceToLocation('conversation', '')
    expect(window.location.search).toBe('')
  })
  it('缺省或未知页面继续打开对话', () => {
    window.history.replaceState(null, '', '/?page=unknown&thread=existing')
    expect(readPageFromLocation()).toBe('conversation')
    expect(readThreadFromLocation()).toBe('existing')
  })
})
