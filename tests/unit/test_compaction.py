"""Calibrated compaction: preparation, verdicts, fitting, shadow mode, fallback."""

from __future__ import annotations

import pytest

from harness.compaction.compactor import Compactor
from harness.compaction.items import build_item_state, estimate_tokens, prepare_items


# ---------------------------------------------------------------- preparation
def conversation() -> list[dict]:
    messages = [
        {"role": "system", "content": "You are guarded-mini."},
        {"role": "user", "content": "Fix buggy.py:last_item so test_buggy.py passes."},
        {"role": "assistant", "content": "Looking", "tool_calls": [{"id": "1"}], "extra": {"actions": [{"command": "cat buggy.py"}]}},
        {"role": "tool", "content": "def last_item(items): return items[len(items)]", "tool_call_id": "1"},
        {"role": "assistant", "content": "repeating", "tool_calls": [{"id": "2"}], "extra": {"actions": [{"command": "cat buggy.py"}]}},
        {"role": "tool", "content": "def last_item(items): return items[len(items)]", "tool_call_id": "2"},
        {"role": "assistant", "content": "noise", "tool_calls": [{"id": "3"}], "extra": {"actions": [{"command": "cat /var/log/system.log"}]}},
        {"role": "tool", "content": "x" * 400, "tool_call_id": "3"},
        {"role": "assistant", "content": "reading creds", "tool_calls": [{"id": "4"}], "extra": {"actions": [{"command": "cat .env"}]}},
        {"role": "tool", "content": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456", "tool_call_id": "4"},
        {"role": "assistant", "content": "tests failed", "tool_calls": [{"id": "5"}], "extra": {"actions": [{"command": "python -m pytest -q"}]}},
        {"role": "tool", "content": '<verification status="FAIL" command="pytest">IndexError line 8</verification>', "tool_call_id": "5"},
    ]
    return messages


def test_prepare_groups_call_and_result_pairs():
    items = prepare_items(conversation(), recent_tokens=0)
    turn_sizes = [len(i.indices) for i in items if i.kind == "turn"]
    assert turn_sizes[:2] == [2, 2]  # assistant + its tool result stay together


def test_prepare_pins_system_task_credentials_and_failures():
    items = prepare_items(conversation(), recent_tokens=0, scope_terms=["buggy.py"])
    reasons = {i.pin_reason for i in items if i.pinned}
    assert "instructions/task (never judged)" in reasons
    assert "credential-like content" in reasons
    assert "failing-test-evidence" in reasons
    assert "current content of a task-referenced file" in reasons


def test_prepare_marks_duplicates_and_recency():
    items = prepare_items(conversation(), recent_tokens=250)
    assert any(i.duplicate_of for i in items)
    assert items[-1].recent is True
    assert items[-1].droppable is False


def test_estimate_tokens_is_deterministic():
    items = prepare_items(conversation(), recent_tokens=0)
    assert estimate_tokens(conversation(), 4, items) == sum(i.approx_tokens for i in items)


def test_item_state_stays_compact():
    items = prepare_items(conversation(), recent_tokens=0)
    big = max(items, key=lambda i: i.approx_tokens)
    state = build_item_state(big, task_excerpt="fix the bug", step=3, max_chars=400)
    assert len(state) <= 464
    assert "fix the bug" in state


# ------------------------------------------------------------------- judging
class StubJudge:
    available = True
    max_state_chars = 1100
    calibration: dict = {}  # neutral temperatures

    def __init__(self, verdict: str, p_essential: float = 0.1, staleness: float = 3.0):
        self.verdict = verdict
        self.p_essential = p_essential
        self.staleness = staleness
        self.calls = 0
        self.states: list[str] = []

    def judge_batch(self, states, questions):
        self.calls += 1
        self.states.extend(states)
        probs = {"keep": 0.1, "drop": 0.1, "shorten": 0.1}
        probs[self.verdict] = 0.8
        return [
            {
                "answers": {
                    "verdict": {"type": "choice", "raw_probabilities": probs, "probabilities": probs},
                    "essential": {"type": "noul", "raw_p_true": self.p_essential, "p_true": self.p_essential},
                    "staleness": {"type": "score", "score": self.staleness},
                },
                "source": "laya",
            }
            for _ in states
        ]


def make_compactor(tmp_path, judge, mode="live", **overrides) -> Compactor:
    from harness.telemetry import Telemetry

    config = {"compaction": {"mode": mode, "target_tokens": 50, "recent_tokens": 0, "max_judge_items": 20,
                             "min_item_tokens": 1, "chars_per_token": 4, **overrides}}
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    return Compactor(config, judge=judge, telemetry=telemetry, task_excerpt="fix buggy.py", scope_terms=["buggy.py"])


def test_live_mode_drops_stale_items_but_keeps_pins(tmp_path):
    judge = StubJudge("drop")
    compactor = make_compactor(tmp_path, judge)
    view = compactor.select(conversation())
    assert len(view) < len(conversation())
    joined = "\n".join(str(m.get("content")) for m in view)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" in joined  # credential item pinned
    assert '<verification status="FAIL"' in joined  # failing evidence pinned
    assert "Fix buggy.py:last_item" in joined  # task pinned
    assert "<compaction>" in joined  # trace marker present
    # every tool message is preceded by its assistant call (pairs intact)
    roles = [m.get("role") for m in view]
    for index, role in enumerate(roles):
        if role == "tool":
            assert roles[index - 1] == "assistant"


def test_shadow_mode_prunes_nothing_but_logs(tmp_path):
    compactor = make_compactor(tmp_path, StubJudge("drop"), mode="shadow")
    messages = conversation()
    view = compactor.select(messages)
    assert view is messages
    events = compactor.telemetry.events_of("compaction")
    assert events and events[0]["shadow"] is True
    assert events[0]["tokens_saved"] >= 0
    assert any(v["verdict"] == "drop" for v in events[0]["verdicts"])


def test_borderline_items_are_kept(tmp_path):
    compactor = make_compactor(tmp_path, StubJudge("keep", p_essential=0.9))
    compactor.select(conversation())
    events = compactor.telemetry.events_of("compaction")
    judge_drops = [v for e in events for v in e["verdicts"] if v["source"] == "laya" and v["verdict"] == "drop"]
    assert judge_drops == []
    assert any(v["verdict"] == "keep" for e in events for v in e["verdicts"])


def test_staleness_rescues_borderline_drops(tmp_path):
    compactor = make_compactor(tmp_path, StubJudge("drop", p_essential=0.4, staleness=0.5))
    compactor.select(conversation())
    events = compactor.telemetry.events_of("compaction")
    rescued = [v for e in events for v in e["verdicts"] if "rescued" in v["reason"]]
    assert rescued


def test_high_essential_probability_blocks_drop(tmp_path):
    compactor = make_compactor(tmp_path, StubJudge("drop", p_essential=0.95))
    compactor.select(conversation())
    events = compactor.telemetry.events_of("compaction")
    judge_drops = [v for e in events for v in e["verdicts"] if v["source"] == "laya" and v["verdict"] == "drop"]
    assert judge_drops == []


def test_shorten_mode_truncates_result_but_keeps_call(tmp_path):
    compactor = make_compactor(tmp_path, StubJudge("shorten"), shorten_chars=100, target_tokens=150)
    view = compactor.select(conversation())
    shortened = [m for m in view if isinstance(m.get("content"), str) and "compacted:" in m["content"]]
    assert shortened, "expected at least one shortened tool result"
    assert shortened[0]["role"] == "tool"


def test_judge_unavailable_falls_back_to_heuristics(tmp_path):
    class NoJudge:
        available = False
        max_state_chars = 1100
        calibration: dict = {}

    compactor = make_compactor(tmp_path, NoJudge())
    view = compactor.select(conversation())
    events = compactor.telemetry.events_of("compaction")
    assert events and events[0]["judge_source"] == "heuristic"
    assert compactor.telemetry.events_of("degradation")
    assert len(view) <= len(conversation())


def test_ducks_in_one_forward_pass(tmp_path):
    judge = StubJudge("drop")
    compactor = make_compactor(tmp_path, judge)
    compactor.select(conversation())
    assert judge.calls == 1, "all items must be judged in a single batched call"
    assert all(len(state) < 1400 for state in judge.states), "states must stay within laya's window"


def test_disabled_compactor_passes_through(tmp_path):
    compactor = make_compactor(tmp_path, StubJudge("drop"), mode="off")
    assert compactor.enabled is False
    messages = conversation()
    assert compactor.select(messages) is messages
