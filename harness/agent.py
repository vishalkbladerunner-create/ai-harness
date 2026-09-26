"""
Harness agent — the upstream loop plus our hooks, without touching upstream.

Why it exists: mini-swe-agent's ``DefaultAgent`` has two seams we need:
``query()`` (what the model sees next -> calibrated compaction + budgets) and
``execute_actions()`` (what runs -> already guarded at the environment level).
Subclassing keeps the vendored core pristine and makes the faculty-round answer
to "what did you build?" precise: this class is ours, the loop is theirs.

Compaction contract (Phase 2): ``compactor.select(full_history)`` returns the
message list the model should see. The canonical ``self.messages`` timeline is
never pruned — it is the audit trail written to the trajectory and the report.

Hackathon criteria served: Phase 2 (calibrated compaction hook), Phase 1.4
(budget enforcement), Phase 4.1 (per-step telemetry).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from minisweagent.agents.default import AgentConfig, DefaultAgent

from harness import HARNESS_NAME, UPSTREAM_COMMIT, UPSTREAM_VERSION, __version__
from harness.budget import BudgetTracker
from harness.secrets import mask_obj

logger = logging.getLogger("harness.agent")


class HarnessAgent(DefaultAgent):
    def __init__(
        self,
        model,
        env,
        *,
        config_class: type = AgentConfig,
        telemetry=None,
        budget_tracker: BudgetTracker | None = None,
        compactor=None,
        **kwargs,
    ):
        super().__init__(model, env, config_class=config_class, **kwargs)
        self.telemetry = telemetry
        self.budget_tracker = budget_tracker
        self.compactor = compactor
        self.step_index = 0

    # ------------------------------------------------------------------ loop
    def query(self) -> dict:
        """Ask the model for the next action, on a compacted view of history."""
        if self.budget_tracker is not None:
            self.budget_tracker.check()

        message = self._query_on_view()
        self.step_index += 1
        usage = (message.get("extra") or {}).get("harness_usage")
        if self.budget_tracker is not None:
            self.budget_tracker.note_call(usage)
            self.budget_tracker.state.steps = self.step_index
        if self.telemetry is not None:
            actions = [a.get("command", "") for a in (message.get("extra") or {}).get("actions", [])]
            self.telemetry.emit(
                "step",
                step=self.step_index,
                n_actions=len(actions),
                actions=[a[:400] for a in actions],
                usage=usage,
                budget=self.budget_tracker.state.snapshot(self.budget_tracker.budget) if self.budget_tracker else None,
            )
        return message

    def _query_on_view(self) -> dict:
        """Run upstream's query() against a possibly-compacted message view.

        The visible list is temporary: whatever the model returns is appended to
        the canonical timeline afterwards, so nothing is ever lost.
        """
        if self.compactor is None or not getattr(self.compactor, "enabled", False):
            return super().query()

        visible = self.compactor.select(self.messages)
        if visible is self.messages or visible is None:
            return super().query()

        original = self.messages
        self.messages = visible
        try:
            message = super().query()
        finally:
            self.messages = original
        self.messages.append(message)
        return message

    # ------------------------------------------------------------- lifecycle
    def execute_actions(self, message: dict) -> list[dict]:
        return super().execute_actions(message)

    def serialize(self, *extra_dicts) -> dict:
        harness_info = {
            "harness": {
                "name": HARNESS_NAME,
                "version": __version__,
                "upstream": {"name": "mini-swe-agent", "version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT},
                "guardrails": getattr(self.env, "policy", None) is not None,
                "compaction": bool(getattr(self.compactor, "enabled", False)),
            }
        }
        return mask_obj(super().serialize(harness_info, *extra_dicts))

    # ------------------------------------------------------------- utilities
    def last_usage(self) -> dict | None:
        for msg in reversed(self.messages):
            usage = (msg.get("extra") or {}).get("harness_usage")
            if usage:
                return usage
        return None

    def write_trajectory(self, path: Path, **extra) -> dict:
        return self.save(path, {"info": {"harness_extra": extra}} if extra else {})


def trajectory_summary(path: Path) -> dict[str, Any]:
    """Small helper for tests/report: read the exit status out of a trajectory."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    info = data.get("info", {})
    return {
        "exit_status": info.get("exit_status", ""),
        "submission": info.get("submission", ""),
        "api_calls": info.get("model_stats", {}).get("api_calls"),
        "messages": len(data.get("messages", [])),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
