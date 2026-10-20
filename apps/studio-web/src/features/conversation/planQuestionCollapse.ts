const PLAN_QUESTION_COLLAPSE_KEY_PREFIX = 'tinkerfin:plan-question-collapse:'

export const planQuestionCollapseKey = (threadId: string) => (
  `${PLAN_QUESTION_COLLAPSE_KEY_PREFIX}${threadId}`
)

const readPlanQuestionCollapseState = (threadId: string): boolean => {
  if (!threadId) return false
  try {
    return window.sessionStorage.getItem(planQuestionCollapseKey(threadId)) === 'collapsed'
  } catch {
    return false
  }
}

const writePlanQuestionCollapseState = (threadId: string, collapsed: boolean): void => {
  if (!threadId) return
  try {
    window.sessionStorage.setItem(
      planQuestionCollapseKey(threadId),
      collapsed ? 'collapsed' : 'expanded',
    )
  } catch {
    // 浏览器禁用会话存储时保留当前页面内的展开收起行为
  }
}

export function readPlanQuestionCollapsed(threadId: string): boolean {
  return readPlanQuestionCollapseState(threadId)
}

export function writePlanQuestionCollapsed(threadId: string, collapsed: boolean): void {
  writePlanQuestionCollapseState(threadId, collapsed)
}

export function clearPlanQuestionCollapsed(threadId: string): void {
  if (!threadId) return
  try {
    window.sessionStorage.removeItem(planQuestionCollapseKey(threadId))
  } catch {
    // 清理失败不应阻止会话删除
  }
}
