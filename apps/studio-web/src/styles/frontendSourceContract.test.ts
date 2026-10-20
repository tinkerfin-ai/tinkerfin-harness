import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const sourceFiles = import.meta.glob('../**/*.{ts,tsx}', {
  eager: true,
  import: 'default',
  query: '?raw',
}) as Record<string, string>

const productionSources = Object.entries(sourceFiles).filter(([path]) => (
  !path.includes('.test.')
  && !path.includes('/test/')
  && !path.endsWith('/vite-env.d.ts')
))

const parse = (path: string, source: string) => ts.createSourceFile(
  path,
  source,
  ts.ScriptTarget.Latest,
  true,
  path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
)

describe('前端源码契约', () => {
  it('所有原生 button 都显式声明 type', () => {
    const missing: string[] = []
    for (const [path, source] of productionSources) {
      const file = parse(path, source)
      const visit = (node: ts.Node) => {
        if (
          (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
          && node.tagName.getText(file) === 'button'
          && !node.attributes.properties.some((property) => (
            ts.isJsxAttribute(property) && property.name.getText(file) === 'type'
          ))
        ) {
          const line = file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1
          missing.push(`${path}:${line}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(file)
    }
    expect(missing).toEqual([])
  })

  it('不使用无所有者 primary、secondary、danger 类名', () => {
    const forbidden: string[] = []
    for (const [path, source] of productionSources) {
      for (const match of source.matchAll(/className="([^"]+)"/g)) {
        const tokens = match[1].split(/\s+/)
        if (tokens.some((token) => ['primary', 'secondary', 'danger'].includes(token))) {
          forbidden.push(`${path}:${match[1]}`)
        }
      }
    }
    expect(forbidden).toEqual([])
  })

  it('业务源码只通过共享 UI 组件使用 Zag 交互能力', () => {
    const directImports: string[] = []
    for (const [path, source] of productionSources) {
      if (path.startsWith('../components/ui/')) continue
      const file = parse(path, source)
      const visit = (node: ts.Node) => {
        const moduleName = (
          (ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
          && node.moduleSpecifier
          && ts.isStringLiteral(node.moduleSpecifier)
        )
          ? node.moduleSpecifier.text
          : (
              ts.isCallExpression(node)
              && node.expression.kind === ts.SyntaxKind.ImportKeyword
              && node.arguments.length === 1
              && ts.isStringLiteral(node.arguments[0])
            )
              ? node.arguments[0].text
              : undefined
        if (moduleName?.startsWith('@zag-js/')) {
          const line = file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1
          directImports.push(`${path}:${line}:${moduleName}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(file)
    }
    expect(directImports).toEqual([])
  })

  it('Studio 单行源码说明不使用全角句号且不存在纯英文 JSDoc', () => {
    const invalidComments: string[] = []
    for (const [path, source] of productionSources) {
      source.split('\n').forEach((line, index) => {
        const trimmed = line.trim()
        if ((trimmed.startsWith('//') || trimmed.startsWith('/**')) && /。(?:\s*\*\/)?$/.test(trimmed)) {
          invalidComments.push(`${path}:${index + 1}:全角句号`)
        }
        if (
          /^\/\*\*\s+[A-Za-z]/.test(trimmed)
          && !/[\u3400-\u9fff]/.test(trimmed)
        ) {
          invalidComments.push(`${path}:${index + 1}:英文 JSDoc`)
        }
      })
    }
    expect(invalidComments).toEqual([])
  })

  it('前端产品提示不以全角句号结尾', () => {
    const invalidCopy: string[] = []
    for (const [path, source] of productionSources) {
      const file = parse(path, source)
      const visit = (node: ts.Node) => {
        if (
          (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
          && /[\u3400-\u9fff]/.test(node.text)
          && /。$/.test(node.text)
        ) {
          const line = file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1
          invalidCopy.push(`${path}:${line}:${node.text}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(file)
    }
    expect(invalidCopy).toEqual([])
  })

  it('生产 JSX 的用户可见中文必须通过语言资源渲染', () => {
    const untranslated: string[] = []
    const hasHan = (value: string) => /[\u3400-\u9fff]/.test(value)
    for (const [path, source] of productionSources) {
      if (path.includes('/i18n/')) continue
      const file = parse(path, source)
      const visit = (node: ts.Node) => {
        if (ts.isJsxText(node) && hasHan(node.text.trim())) {
          const line = file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1
          untranslated.push(`${path}:${line}:${node.text.trim()}`)
        }
        if (
          ts.isJsxAttribute(node)
          && node.initializer
          && ts.isStringLiteral(node.initializer)
          && hasHan(node.initializer.text)
        ) {
          const line = file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1
          untranslated.push(`${path}:${line}:${node.initializer.text}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(file)
    }
    expect(untranslated).toEqual([])
  })
})
