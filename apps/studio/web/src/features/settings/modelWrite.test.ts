import { describe, expect, it } from 'vitest'
import { modelWrite, newModel } from './useModelSettings'

describe('模型配置写入', () => {
  it('保留完整人工配置并排除只读能力资料', () => {
    const model = { ...newModel('owned'), image_support: 'unsupported' as const }
    const payload = modelWrite(model)
    expect(payload).not.toHaveProperty('image_input_capability')
    expect(payload).toEqual({ model_id: model.model_id, connection_id: 'owned', display_name: '', model_name: '', image_support: 'unsupported', reasoning_enabled: false, enabled: true, is_default: false, sort_order: 0, chat_options: model.chat_options })
  })
})
