type InteractionCardTone = 'plan' | 'warning'

export function InteractionCardColorBridge({ tone }: { tone: InteractionCardTone }) {
  return <div className={`interaction-card-color-bridge is-${tone}`} aria-hidden="true" />
}
