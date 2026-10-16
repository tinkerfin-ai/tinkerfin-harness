import type { Message } from '../../../types'

/** 已加载对话的阅读入口，消息 ID 在补载历史后仍标识同一提问 */
export interface ConversationTurn {
  messageId: string
  prompt: string
  response: string
}

const excerpt = (text: string) => text.slice(0, 640).replace(/\s+/g, ' ').trim().slice(0, 160)

/** 只收录用户提问和主对话公开回答，忽略起始提问尚未加载的片段 */
export function conversationTurns(messages: Message[]): ConversationTurn[] {
  const turns: ConversationTurn[] = []
  for (const message of messages) {
    if (message.meta?.sourceAgentName) continue
    if (message.role === 'user') {
      turns.push({ messageId: message.id, prompt: excerpt(message.content), response: '' })
    } else if (message.role === 'assistant' && message.content.trim() && turns.length) {
      turns[turns.length - 1].response = excerpt(message.content)
    }
  }
  return turns
}
