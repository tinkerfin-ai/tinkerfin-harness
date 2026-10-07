/** jsdom 缺少原生浮层接口，单元测试只模拟开闭；焦点与布局由浏览器验证 */
export function mockNativePopover() {
  const names = ['matches', 'showPopover', 'hidePopover', 'scrollIntoView'] as const
  const originals = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(HTMLElement.prototype, name)]))
  const nativeMatches = HTMLElement.prototype.matches
  const open = new WeakSet<HTMLElement>()
  const toggle = (element: HTMLElement, visible: boolean) => {
    const wasOpen = open.has(element)
    if (visible === wasOpen) return
    if (visible) open.add(element)
    else open.delete(element)
    element.dispatchEvent(Object.assign(new Event('toggle'), { oldState: wasOpen ? 'open' : 'closed', newState: visible ? 'open' : 'closed' }))
  }
  Object.defineProperties(HTMLElement.prototype, {
    matches: { configurable: true, value: function (this: HTMLElement, selector: string) { return selector === ':popover-open' ? open.has(this) : nativeMatches.call(this, selector) } },
    showPopover: { configurable: true, value: function (this: HTMLElement) { toggle(this, true) } },
    hidePopover: { configurable: true, value: function (this: HTMLElement) { toggle(this, false) } },
    scrollIntoView: { configurable: true, value: () => {} },
  })
  return () => {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor)
      else Reflect.deleteProperty(HTMLElement.prototype, name)
    }
  }
}
