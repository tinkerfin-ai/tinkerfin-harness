import { describe, expect, it } from 'vitest'
import { parseServiceOptions } from './modelOptions'

describe('服务 JSON 参数契约', () => {
  it.each(['[]', 'null', '"text"', '2'])('拒绝非对象 %s', (text) => expect(parseServiceOptions(text).issues[0]?.code).toBe('object_required'))
  it.each(['{', '{"x":1,}', '{/* note */"x":1}'])('严格检查 JSON 语法 %s', (text) => expect(parseServiceOptions(text).issues[0]?.code).toBe('syntax'))
  it('定位嵌套重复键', () => {
    const text = '{"x":{"a":1,"a":2}}'
    const issue = parseServiceOptions(text).issues[0]
    expect(issue?.code).toBe('duplicate')
    expect(text.slice(issue?.from, issue?.to)).toBe('"a"')
  })
  it.each(['model', 'prompt', 'n', 'api_key', 'Authorization'])('禁止受控键 %s', (key) => expect(parseServiceOptions(JSON.stringify({[key]: 'x'}), ['model', 'prompt', 'n', 'api_key', 'authorization']).issues[0]?.code).toBe('reserved'))
  it('拒绝无穷大', () => expect(parseServiceOptions('{"value":1e999}').issues[0]?.code).toBe('nonfinite'))
  it('按 UTF-8 字节限制编辑器内容', () => expect(parseServiceOptions(JSON.stringify({notes: '汉'.repeat(23_000)})).issues[0]?.code).toBe('too_large'))

})

it('过深输入在解析前得到可恢复错误', () => {
  expect(parseServiceOptions('{"x":'.repeat(33) + '1' + '}'.repeat(33)).issues[0]?.code).toBe('too_deep')
  expect(parseServiceOptions('{"x":'.repeat(32) + '1' + '}'.repeat(32)).issues).toEqual([])
})
