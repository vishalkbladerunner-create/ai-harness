"""Report generation is a pure function of a context dict (Phase 4.2)."""

from __future__ import annotations

from harness.report import render_report


def minimal_ctx(**overrides) -> dict:
    ctx = {
        "run_id": "unit-run",
        "status": "submitted",
        "exit_status": "Submitted",
        "workspace": "/tmp/ws",
        "model_name": "openai/test-model",
        "started_iso": "2026-01-01T00:00:00",
        "duration_s": 12.3,
        "issue": {"source": "stdin", "chars": 10, "text": "fix the bug", "paths": ["a.py"], "identifiers": ["foo"], "tests": []},
        "stats": {"steps": 3, "llm_calls": 3, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "wall_seconds": 1.0},
        "patch": {"files": [{"status": "M", "path": "a.py", "additions": "1", "deletions": "1"}], "diff": "-old\n+new", "path": "/tmp/patch.diff"},
        "verification": {"command": "python -m pytest -q", "kind": "python", "history": [{"reason": "final", "ok": True, "returncode": 0, "duration_s": 0.5}], "last": {"output_tail": "1 passed"}},
        "guardrails": [{"n": 1, "decision": "refuse", "policy": {"source": "deterministic", "reason": "rm -rf", "probability": 0.99}, "command": "rm -rf /"}],
        "degradations": [{"component": "laya", "reason": "checkpoint missing"}],
        "budget": {"max_steps": 60},
        "timeline": [{"iso": "2026-01-01T00:00:01", "kind": "model_call", "usage": {"total_tokens": 15}, "latency_s": 0.4}],
        "submission": "done",
        "telemetry_path": "/tmp/t.jsonl",
        "upstream_commit": "a83fcae",
    }
    ctx.update(overrides)
    return ctx


def test_report_contains_required_sections():
    text = render_report(minimal_ctx())
    for heading in (
        "# guarded-mini run report",
        "## Issue",
        "## Summary",
        "## Files changed",
        "## Verification evidence",
        "## Guardrail log",
        "## Degradation notes",
        "## Budget",
        "## Timeline",
        "## Submission",
    ):
        assert heading in text, heading
    assert "guarded-mini" in text
    assert "1 passed" in text
    assert "a83fcae" in text


def test_report_handles_empty_guardrails_and_verification():
    text = render_report(minimal_ctx(guardrail_decisions=[], verification={}, patch={}, degradations=[]))
    assert "No commands were executed" in text
    assert "No test command detected" in text
    assert "no patch captured" in text


def test_report_marks_budget_exhausted_status():
    text = render_report(minimal_ctx(status="limits_exceeded", exit_status="LimitsExceeded"))
    assert "BUDGET EXHAUSTED" in text


def test_report_shows_the_failure_reason():
    text = render_report(minimal_ctx(status="error", exit_status="", error="RuntimeError: model endpoint reports insufficient balance/quota (HTTP 402)"))
    assert "## Error" in text
    assert "insufficient balance" in text


def test_report_masks_credentials_in_issue_text(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_TOKEN", "supersecretvalue123")
    text = render_report(
        minimal_ctx(issue={"source": "stdin", "chars": 10, "text": "token supersecretvalue123", "paths": [], "identifiers": [], "tests": []})
    )
    assert "supersecretvalue123" not in text
