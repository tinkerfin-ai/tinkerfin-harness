"""从公开运行事实提供对话失败提示，不改变执行或链路状态"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from tinkerfin_tracing import RunFact, TraceSemanticFact

FAILURE_PROJECTION = "studio.conversation.failures"


class ConversationRunFailure(BaseModel):
    """运行的失败事实；普通提问可根据执行前失败证明重新发送"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    run_id: str = Field(alias="runId")
    error_code: str | None = Field(alias="errorCode")
    failed_at: datetime = Field(alias="failedAt")
    retryable: bool = Field(description="仅明确发生在 Agent 执行前的失败可重新发送")


class ConversationFailures(BaseModel):
    """固定 Trace 前缀中的运行失败记录"""

    failures: tuple[ConversationRunFailure, ...] = ()


class FailureProjectionState(BaseModel):
    """保存普通提问身份与已确认失败，供框架缓存和重放"""

    ordinary_runs: set[str] = Field(default_factory=set)
    failures: dict[str, ConversationRunFailure] = Field(default_factory=dict)


class ConversationFailureProjection:
    """使用框架业务投影提供失败原因，不推断缺失的执行结果"""

    name = FAILURE_PROJECTION
    state_type = FailureProjectionState
    result_type = ConversationFailures

    def initial_state(self) -> FailureProjectionState:
        return FailureProjectionState()

    def apply(
        self, state: FailureProjectionState, fact: TraceSemanticFact
    ) -> FailureProjectionState:
        if not isinstance(fact, RunFact):
            return state
        run_id = fact.identity.run_id
        if fact.phase == "started" and fact.input_kind in {"ordinary", "branch"}:
            return state.model_copy(
                update={"ordinary_runs": state.ordinary_runs | {run_id}}
            )
        if fact.phase != "terminal":
            return state
        failures = dict(state.failures)
        if fact.outcome == "failed":
            failures[run_id] = ConversationRunFailure(
                runId=run_id,
                errorCode=fact.code,
                failedAt=fact.occurred_at,
                retryable=run_id in state.ordinary_runs
                and fact.code
                in {
                    "runtime_initialization_error",
                    "workspace_busy",
                    "workspace_file_conflict",
                    "workspace_stopped",
                    "workspace_paused",
                    "workspace_unavailable",
                    "workspace_timeout",
                },
            )
        return state.model_copy(
            update={
                "ordinary_runs": state.ordinary_runs - {run_id},
                "failures": failures,
            }
        )

    def finish(self, state: FailureProjectionState) -> ConversationFailures:
        return ConversationFailures(failures=tuple(state.failures.values()))


def visible_run_failures(
    result: object, run_ids: set[str]
) -> tuple[ConversationRunFailure, ...]:
    """仅返回所选历史窗口与当前运行的失败，不混入其他分页"""

    return tuple(
        failure
        for failure in ConversationFailures.model_validate(result).failures
        if failure.run_id in run_ids
    )
