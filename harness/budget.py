"""
Budget tracker — hard token/step/time limits with a graceful exit.

Why it exists: upstream mini-swe-agent budgets on dollars, which is meaningless
on an unknown OpenAI-compatible endpoint (no price table). Phase 1.4 requires a
hard token/step/time budget with best-effort exit and a summary, so this class
owns the numbers and raises upstream's ``LimitsExceeded`` — the documented
graceful-exit path — when one is exhausted.

Hackathon criteria served: Phase 1.4 (resource guardrails).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from minisweagent.exceptions import LimitsExceeded


@dataclass
class Budget:
    max_steps: int = 0
    max_llm_calls: int = 0
    max_prompt_tokens: int = 0
    max_completion_tokens: int = 0
    max_wall_seconds: int = 0

    @classmethod
    def from_config(cls, config: dict) -> "Budget":
        cfg = config or {}
        return cls(
            max_steps=int(cfg.get("max_steps", 0) or 0),
            max_llm_calls=int(cfg.get("max_llm_calls", 0) or 0),
            max_prompt_tokens=int(cfg.get("max_prompt_tokens", 0) or 0),
            max_completion_tokens=int(cfg.get("max_completion_tokens", 0) or 0),
            max_wall_seconds=int(cfg.get("max_wall_seconds", 0) or 0),
        )


@dataclass
class BudgetState:
    start: float = field(default_factory=time.time)
    steps: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_usage: bool = False

    def snapshot(self, budget: Budget) -> dict:
        return {
            "steps": self.steps,
            "max_steps": budget.max_steps,
            "llm_calls": self.llm_calls,
            "max_llm_calls": budget.max_llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "max_prompt_tokens": budget.max_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "max_completion_tokens": budget.max_completion_tokens,
            "wall_seconds": round(time.time() - self.start, 1),
            "max_wall_seconds": budget.max_wall_seconds,
        }


class BudgetTracker:
    def __init__(self, budget: Budget, telemetry=None):
        self.budget = budget
        self.state = BudgetState()
        self.telemetry = telemetry

    def note_call(self, usage: dict | None) -> None:
        self.state.llm_calls += 1
        if usage:
            self.state.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.state.completion_tokens += int(usage.get("completion_tokens") or 0)
            if usage.get("estimated"):
                self.state.estimated_usage = True

    def check(self) -> None:
        """Raise upstream's graceful-exit exception when a hard limit is hit."""
        b, s = self.budget, self.state
        wall = time.time() - s.start
        reason = ""
        if 0 < b.max_wall_seconds <= wall:
            reason = f"wall-clock budget exhausted ({wall:.0f}s >= {b.max_wall_seconds}s)"
        elif 0 < b.max_steps <= s.steps:
            reason = f"step budget exhausted ({s.steps} >= {b.max_steps})"
        elif 0 < b.max_llm_calls <= s.llm_calls:
            reason = f"model-call budget exhausted ({s.llm_calls} >= {b.max_llm_calls})"
        elif 0 < b.max_prompt_tokens <= s.prompt_tokens:
            reason = f"prompt-token budget exhausted ({s.prompt_tokens} >= {b.max_prompt_tokens})"
        elif 0 < b.max_completion_tokens <= s.completion_tokens:
            reason = f"completion-token budget exhausted ({s.completion_tokens} >= {b.max_completion_tokens})"
        if not reason:
            return
        snapshot = self.state.snapshot(self.budget)
        if self.telemetry is not None:
            self.telemetry.emit("budget_exhausted", reason=reason, budget=snapshot)
        raise LimitsExceeded(
            {
                "role": "exit",
                "content": f"Budget exhausted: {reason}. Best-effort exit; see the run report for partial results.",
                "extra": {"exit_status": "LimitsExceeded", "submission": "", "budget_reason": reason, "budget": snapshot},
            }
        )
