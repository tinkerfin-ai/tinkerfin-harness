import { createContext } from 'react'

/** 当前工作台拥有的正文显示进度，以稳定消息身份索引，不写入历史数据 */
export const TextRevealProgressContext = createContext<Map<string, string> | null>(null)
