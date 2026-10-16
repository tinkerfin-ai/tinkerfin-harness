"""Plan 历史只展示已经结算的原始用户决定"""

import pytest

from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
)
from tinkerfin_studio.conversation.plan_results import saved_plan_results


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "answered", "answerType": "single_choice", "optionId": "bottom"},
        {"status": "answered", "answerType": "single_choice", "customAnswer": "自定义"},
        {
            "status": "answered",
            "answerType": "multiple_choice",
            "optionIds": ["a", "b"],
            "customAnswer": "补充",
        },
        {"status": "answered", "answerType": "text", "answer": "今天也要开心呀"},
        {"status": "answered", "answerType": "date", "date": "2026-10-10"},
        {"status": "answered", "answerType": "time", "time": "09:30:00"},
        {
            "status": "answered",
            "answerType": "datetime",
            "dateTime": "2026-10-10T09:30:00",
        },
        {"status": "skipped"},
    ],
)
def test_saved_answers_preserve_the_submitted_values(answer):
    claim = ConversationInterruptClaim(
        interrupt_id="question",
        claimed_run_id="resume",
        status="resolved",
        resolution_id="receipt",
    )
    run = ConversationRunRegistration(
        run_id="resume",
        input_json={
            "resume": [
                {
                    "interruptId": "question",
                    "status": "resolved",
                    "payload": {
                        "type": "respond",
                        "answers": {"choice": answer},
                    },
                }
            ],
            "private_context": "not public",
        },
    )
    (result,) = saved_plan_results([claim], {"resume": run}, frozenset({"question"}))
    assert result.outcome == "answered"
    assert result.answers is not None
    assert (
        result.answers["choice"].model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        == answer
    )
    assert "private_context" not in result.model_dump_json()


@pytest.mark.parametrize(
    "action,outcome",
    [
        ("approve", "approved"),
        ("reject", "rejected"),
        ("dismiss", "dismissed"),
        ("cancel", "cancelled"),
        ("discuss", "discussed"),
        ("respond", "discussed"),
    ],
)
def test_saved_review_reports_the_actual_decision(action, outcome):
    claim = ConversationInterruptClaim(
        interrupt_id="review",
        claimed_run_id="resume",
        status="resolved",
        resolution_id="receipt",
    )
    run = ConversationRunRegistration(
        run_id="resume",
        input_json={
            "resume": [
                {
                    "interruptId": "review",
                    "status": "resolved",
                    "payload": {
                        "type": action,
                        "baseRevision": 1,
                        "message": "保留原图",
                    },
                }
            ]
        },
    )
    (result,) = saved_plan_results([claim], {"resume": run}, frozenset({"review"}))
    assert result.outcome == outcome
    assert result.reason == (
        "保留原图" if action in {"reject", "discuss", "respond"} else None
    )
    assert result.answers is None


def test_unknown_or_unrelated_claims_never_become_plan_results():
    claims = [
        ConversationInterruptClaim(
            interrupt_id=identifier,
            claimed_run_id="resume",
            status=status,
            resolution_id=receipt,
        )
        for identifier, status, receipt in [
            ("pending", "claimed", None),
            ("uncertain", "resolved", None),
            ("tool", "resolved", "tool-receipt"),
            ("cancelled", "cancelled", "cancel-receipt"),
        ]
    ]
    (result,) = saved_plan_results(
        claims, {}, frozenset({"pending", "uncertain", "cancelled"})
    )
    assert result.interrupt_id == "cancelled"
    assert result.outcome == "cancelled"
