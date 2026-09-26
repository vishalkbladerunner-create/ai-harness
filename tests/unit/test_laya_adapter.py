"""laya adapter: the checkpoint must load once, and a failed load must not retry.

Both were real defects: every judge batch reloaded the checkpoint (5 loads in a
3-step run) and a failing load was retried on every call with the full timeout.
"""

from __future__ import annotations

from harness.laya.adapter import LayaJudge


class _FakeAgent:
    def __init__(self):
        self.batches = 0

    def predict_batch(self, states, questions, max_len=None, head_max_len=None):
        self.batches += 1
        return [{"answers": {}, "model": "fake"} for _ in states]


def test_loaded_judge_is_not_reloaded(monkeypatch):
    judge = LayaJudge(config={}, telemetry=None)
    agent = _FakeAgent()
    judge._agent = agent
    judge._load_attempted = True

    def fail():
        raise AssertionError("checkpoint must not be loaded twice")

    monkeypatch.setattr(judge, "_load", fail)
    judge.judge_batch(["state"], {})
    judge.judge_batch(["state"], {})
    assert agent.batches == 2
    assert judge.calls == 2


def test_failed_load_is_not_retried(monkeypatch):
    judge = LayaJudge(config={}, telemetry=None)
    calls: list[int] = []
    monkeypatch.setattr(judge, "_load", lambda: calls.append(1) or False)
    assert judge.judge_batch(["state"], {})[0]["source"] == "unavailable"
    assert judge.judge_batch(["state"], {})[0]["source"] == "unavailable"
    assert calls == [1]
