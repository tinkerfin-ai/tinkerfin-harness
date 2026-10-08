import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { WorkspaceHeader } from './WorkspaceHeader'

describe('WorkspaceHeader', () => {
  it('keeps the current conversation title available for responsive presentation', () => {
    render(
      <WorkspaceHeader
        conversationTitle="研究下一季度产品路线"
        overlayTriggerRef={createRef<HTMLButtonElement>()}
        onOpenOverlay={vi.fn()}
        actions={<button type="button">额外操作</button>}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '研究下一季度产品路线' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '额外操作' })).toBeInTheDocument()
  })

  it('uses the product term for an untitled draft', () => {
    render(
      <WorkspaceHeader
        conversationTitle=""
        overlayTriggerRef={createRef<HTMLButtonElement>()}
        onOpenOverlay={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: '新会话' })).toBeInTheDocument()
  })
})
