"""从已结算认领和原始提交恢复用户可见的 Plan 决定"""

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tinkerfin.plan.clarification import BuiltInResponse, SkippedResponse
from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
)


class ConversationPlanResult(BaseModel):
    """已核实保存的答案或决定；不会以请求受理推断执行成功"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    interrupt_id: str = Field(alias="interruptId")
    submission_run_id: str = Field(alias="submissionRunId")
    outcome: Literal[
        "answered", "approved", "rejected", "dismissed", "cancelled", "discussed"
    ]
    answers: dict[str, BuiltInResponse | SkippedResponse] | None = None
    reason: str | None = None


def saved_plan_results(
    claims: Sequence[ConversationInterruptClaim],
    runs: Mapping[str, ConversationRunRegistration],
    plan_ids: frozenset[str],
) -> tuple[ConversationPlanResult, ...]:
    """只投影有持久化凭据的可见 Plan 交互，不公开其他请求内容"""

    results: list[ConversationPlanResult] = []
    for claim in claims:
        if claim.interrupt_id not in plan_ids or not claim.resolution_id:
            continue
        if claim.status == "cancelled":
            results.append(
                ConversationPlanResult(
                    interruptId=claim.interrupt_id,
                    submissionRunId=claim.claimed_run_id,
                    outcome="cancelled",
                )
            )
            continue
        if claim.status != "resolved":
            continue
        run = runs.get(claim.claimed_run_id)
        if run is None:
            continue
        entries = run.input_json.get("resume")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or entry.get("interruptId") != claim.interrupt_id
            ):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            outcomes = {
                "respond": "answered",
                "approve": "approved",
                "reject": "rejected",
                "dismiss": "dismissed",
                "cancel": "cancelled",
                "discuss": "discussed",
            }
            action = payload.get("type")
            if not isinstance(action, str) or action not in outcomes:
                continue
            outcome = outcomes[action]
            if action == "respond" and "answers" not in payload:
                outcome = "discussed"
            results.append(
                ConversationPlanResult.model_validate(
                    {
                        "interruptId": claim.interrupt_id,
                        "submissionRunId": claim.claimed_run_id,
                        "outcome": outcome,
                        "answers": payload.get("answers")
                        if action == "respond"
                        else None,
                        "reason": payload.get("message")
                        if outcome in {"rejected", "discussed"}
                        else None,
                    }
                )
            )
    return tuple(results)
