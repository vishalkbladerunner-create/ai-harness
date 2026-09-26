"""Budget enforcement: hard limits raise upstream's graceful-exit exception."""

from __future__ import annotations

import pytest

from harness.budget import Budget, BudgetTracker
from minisweagent.exceptions import LimitsExceeded


def make(telemetry=None, **kwargs) -> BudgetTracker:
    return BudgetTracker(Budget(**kwargs), telemetry)


def test_no_limits_never_raises():
    tracker = make(max_steps=0, max_wall_seconds=0)
    tracker.check()


def test_step_budget_raises_with_snapshot():
    tracker = make(max_steps=2)
    tracker.state.steps = 2
    with pytest.raises(LimitsExceeded) as excinfo:
        tracker.check()
    message = excinfo.value.messages[0]
    assert message["extra"]["exit_status"] == "LimitsExceeded"
    assert "step budget" in message["content"]
    assert message["extra"]["budget"]["steps"] == 2


def test_token_budgets_accumulate_and_fire():
    tracker = make(max_prompt_tokens=10, max_completion_tokens=5)
    tracker.note_call({"prompt_tokens": 4, "completion_tokens": 2, "estimated": True})
    tracker.check()
    tracker.note_call({"prompt_tokens": 7, "completion_tokens": 4})
    with pytest.raises(LimitsExceeded):
        tracker.check()
    assert tracker.state.estimated_usage is True


def test_llm_call_budget():
    tracker = make(max_llm_calls=1)
    tracker.note_call(None)
    with pytest.raises(LimitsExceeded) as excinfo:
        tracker.check()
    assert "model-call budget" in excinfo.value.messages[0]["content"]


def test_budget_event_emitted_on_telemetry():
    from harness.telemetry import Telemetry

    telemetry = Telemetry(__import__("pathlib").Path("/tmp/budget-telemetry.jsonl"), "unit")
    tracker = BudgetTracker(Budget(max_steps=1), telemetry)
    tracker.state.steps = 1
    with pytest.raises(LimitsExceeded):
        tracker.check()
    assert telemetry.events_of("budget_exhausted")
    telemetry.close()
