import { createContext } from 'react'

/** 保持代码块组件身份稳定，状态变化只更新其展示内容 */
export const MarkdownStreamingContext = createContext(false)
