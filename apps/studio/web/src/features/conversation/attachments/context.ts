import { createContext } from 'react'
import type { Attachment } from './content'

export const AttachmentReferenceContext = createContext<
  ((attachment: Attachment) => void) | null
>(null)
