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
        # Harness notices queued for the NEXT model call (budget warnings, stall
        # nudges): they are appended to the canonical timeline as user messages,
        # so the trajectory shows exactly what the model was told and when.
        self._pending_notices: list[str] = []
        self._last_action_sig = ""
        self._stall_repeats = 0
        self._stall_nudged = False
        self._stall_exit = ""

    # ------------------------------------------------------------------ loop
    def query(self) -> dict:
        """Ask the model for the next action, on a compacted view of history."""
        if self.budget_tracker is not None:
            self.budget_tracker.check()
        if self._stall_exit:
            self._raise_stuck()
        self._flush_notices()

        message = self._query_on_view()
        self.step_index += 1
        usage = (message.get("extra") or {}).get("harness_usage")
        if self.budget_tracker is not None:
            self.budget_tracker.note_call(usage)
            self.budget_tracker.state.steps = self.step_index
        actions = [a.get("command", "") for a in (message.get("extra") or {}).get("actions", [])]
        if self.budget_tracker is not None:
            for warning in self.budget_tracker.warnings():
                self._pending_notices.append(
                    f"resource update: {warning}. If the task is nearly done, verify and submit now "
                    "(harness_submit_patch); otherwise continue, preferring the shortest path to a verified fix."
                )
        self._track_stall(actions)
        if self.telemetry is not None:
            self.telemetry.emit(
                "step",
                step=self.step_index,
                n_actions=len(actions),
                actions=[a[:400] for a in actions],
                usage=usage,
                budget=self.budget_tracker.state.snapshot(self.budget_tracker.budget) if self.budget_tracker else None,
            )
        return message

    # ------------------------------------------------------------- notices
    def _flush_notices(self) -> None:
        """Deliver queued harness notices as one visible user message."""
        if not self._pending_notices:
            return
        content = (
            "<harness_notice>\n"
            + "\n".join(f"- {note}" for note in self._pending_notices)
            + "\n</harness_notice>\n(This message comes from the harness, not the user or the issue.)"
        )
        self._pending_notices = []
        self.messages.append({"role": "user", "content": content})
        if self.telemetry is not None:
            self.telemetry.emit("harness_notice", content=content[:1000])

    def _track_stall(self, actions: list[str]) -> None:
        """Nudge on a repeated identical action; give up at twice the limit.

        With budgets defaulting to unlimited this is the real runaway guard:
        a model stuck issuing the exact same command forever is detected after
        ``stall_duplicate_limit`` consecutive repeats, nudged once, and exited
        gracefully after twice that.
        """
        limit = self.budget_tracker.budget.stall_duplicate_limit if self.budget_tracker else 0
        sig = "\n".join(a.strip() for a in actions if a.strip())
        if not limit or not sig:
            self._stall_repeats = 0
            self._last_action_sig = ""
            self._stall_nudged = False
            return
        if sig == self._last_action_sig:
            self._stall_repeats += 1
        else:
            self._stall_repeats = 0
            self._stall_nudged = False
            self._stall_exit = ""
        self._last_action_sig = sig
        consecutive = self._stall_repeats + 1
        if consecutive >= 2 * limit:
            self._stall_exit = (
                f"stuck: the identical action was repeated {consecutive} times in a row without progress "
                f"({sig[:200]!r})"
            )
        elif consecutive >= limit and not self._stall_nudged:
            self._stall_nudged = True
            self._pending_notices.append(
                f"you have issued the identical command {consecutive} times in a row ({sig[:200]!r}) — "
                "repeating it will not change the result. Analyse why it failed, try a different approach, "
                "or submit what you have. If this repetition continues the run will be stopped as stuck."
            )
            if self.telemetry is not None:
                self.telemetry.emit("stall_warning", consecutive=consecutive, command=sig[:400])

    def _raise_stuck(self) -> None:
        from minisweagent.exceptions import LimitsExceeded

        reason = self._stall_exit
        if self.telemetry is not None:
            self.telemetry.emit("budget_exhausted", reason=reason, budget=None)
        raise LimitsExceeded(
            {
                "role": "exit",
                "content": f"Run stopped: {reason}. Best-effort exit; see the run report for partial results.",
                "extra": {"exit_status": "LimitsExceeded", "submission": "", "budget_reason": reason},
            }
        )

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
