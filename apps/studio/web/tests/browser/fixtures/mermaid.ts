export const flowchart = 'flowchart LR\n  A[输入问题] --> B[分析需求]\n  B --> C{需要工具?}\n  C -->|是| D[调用工具]\n  C -->|否| E[生成回复]\n  D --> E'
export const diagrams = [
  flowchart,
  'sequenceDiagram\n  participant U as 用户\n  participant A as 智能体\n  U->>A: 分析需求\n  A-->>U: 返回结果',
  'classDiagram\n  class Account {\n    +String name\n    +deposit(amount)\n  }\n  Customer --> Account',
  'stateDiagram-v2\n  [*] --> Ready\n  Ready --> Done\n  Done --> [*]',
  'erDiagram\n  CUSTOMER ||--o{ ORDER : places',
  'gantt\n  title 项目排期\n  dateFormat YYYY-MM-DD\n  todayMarker off\n  section 交付\n  实现 :a, 2026-09-01, 3d',
  'pie title 任务分布\n  "开发" : 60\n  "验证" : 40',
  'mindmap\n  root((项目))\n    设计\n    实现\n    验证',
]
export const fence = (source: string, language = 'mermaid') => `\`\`\`${language}\n${source}\n\`\`\``
