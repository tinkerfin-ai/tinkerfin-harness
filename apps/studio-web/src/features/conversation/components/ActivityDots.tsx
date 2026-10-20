export function ActivityDots({ label = '正在进行' }: { label?: string }) {
  return <span className="activity-dots" role="status" aria-label={label}><i /><i /><i /></span>
}
