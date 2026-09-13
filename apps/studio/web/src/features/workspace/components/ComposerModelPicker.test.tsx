import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ComposerModelPicker } from './ComposerModelPicker'

describe('ComposerModelPicker', () => {
  it('opens the model list with the shared rotating chevron', () => {
    function Example() {
      const [open, setOpen] = useState(false)
      return <ComposerModelPicker model="model-a" modelIds={['model-a', 'model-b']}
        defaultModelId="model-a" modelDisplayName={value => value} status="ready"
        open={open} onOpenChange={setOpen} onSelectModel={vi.fn()} onRetry={vi.fn()} />
    }
    render(<Example />)

    const trigger = screen.getByRole('button', { name: '选择模型' })
    expect(trigger.querySelector('.ui-compact-picker-chevron')).not.toBeNull()
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('listbox', { name: '模型选项' })).toBeVisible()
  })
})
