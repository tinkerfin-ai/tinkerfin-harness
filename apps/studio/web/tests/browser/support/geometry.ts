import type { ElementHandle, Locator } from '@playwright/test'

/** 同时读取同一页面主文档中的可见边界，避免滚动或布局变化混入比较 */
export async function measureBounds(...locators: [Locator, ...Locator[]]) {
  const page = locators[0].page()
  const elements: ElementHandle<HTMLElement | SVGElement>[] = []
  try {
    for (const locator of locators) {
      if (locator.page() !== page) throw new Error('边界比较必须来自同一页面')
      const element = await locator.elementHandle()
      if (!element) throw new Error('边界比较的元素不存在')
      elements.push(element)
    }
    return await page.evaluate(elements => elements.map(element => {
      if (!element.isConnected || element.ownerDocument !== document) {
        throw new Error('边界比较的元素必须位于当前页面主文档')
      }
      const { x, y, width, height } = element.getBoundingClientRect()
      const { visibility } = getComputedStyle(element)
      if (width <= 0 || height <= 0 || visibility === 'hidden' || visibility === 'collapse') {
        throw new Error('边界比较的元素必须可见')
      }
      return { x, y, width, height }
    }), elements)
  } finally {
    await Promise.all(elements.map(element => element.dispose()))
  }
}
