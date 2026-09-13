import { DocumentPreviewError, type OfficeFormat, type OfficePreviewResponse } from './documentPreview'
import { parseOfficeDocument } from './parseOfficeDocument'

self.onmessage = async (event: MessageEvent<{ bytes: ArrayBuffer; format: OfficeFormat }>) => {
  let response: OfficePreviewResponse
  try { response = { content: await parseOfficeDocument(event.data.bytes, event.data.format) } }
  catch (error) { response = { error: error instanceof DocumentPreviewError ? error.code : 'invalid' } }
  self.postMessage(response)
}
