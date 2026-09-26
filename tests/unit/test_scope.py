"""Scope guardrail: classification, conservative rollback, telemetry."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.guardrails.scope import ScopeGuard
from harness.issue import parse_issue


def git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "buggy.py").write_text("def last(items):\n    return items[len(items)]\n", encoding="utf-8")
    (ws / "test_buggy.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (ws / "unrelated.cfg").write_text("x=1\n", encoding="utf-8")
    git(ws, "init", "-q")
    git(ws, "config", "user.email", "t@example.invalid")
    git(ws, "config", "user.name", "t")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "base")
    return ws


ISSUE = parse_issue("Fix buggy.py:last_item so test_buggy passes. See test_buggy.py.\n")


def changed(path: str, status: str = "M") -> dict:
    return {"path": path, "status": status, "additions": "1", "deletions": "1"}


def test_in_scope_when_named_by_issue(tmp_path):
    ws = make_repo(tmp_path)
    guard = ScopeGuard(ws, ISSUE)
    summary = guard.enforce([changed("src/buggy.py")])
    assert summary["in_scope"] == 1
    assert summary["flagged"] == []


def test_test_file_is_in_scope(tmp_path):
    ws = make_repo(tmp_path)
    guard = ScopeGuard(ws, ISSUE)
    summary = guard.enforce([changed("test_buggy.py")])
    assert summary["flagged"] == []


def test_out_of_scope_file_is_flagged_and_rolled_back(tmp_path):
    ws = make_repo(tmp_path)
    (ws / "unrelated.cfg").write_text("x=2\n", encoding="utf-8")
    guard = ScopeGuard(ws, ISSUE, rollback=True)
    # src/buggy.py anchors the scope (deterministically in scope), so unrelated.cfg
    # is rolled back; without an anchor the same verdict would be report-only.
    summary = guard.enforce([changed("src/buggy.py"), changed("unrelated.cfg")])
    assert summary["flagged"] == ["unrelated.cfg"]
    assert summary["rolled_back"] and summary["rolled_back"][0]["path"] == "unrelated.cfg"
    assert (ws / "unrelated.cfg").read_text() == "x=1\n"  # restored


def test_without_any_anchor_rollback_is_report_only(tmp_path):
    ws = make_repo(tmp_path)
    (ws / "unrelated.cfg").write_text("x=2\n", encoding="utf-8")
    guard = ScopeGuard(ws, ISSUE, rollback=True)
    summary = guard.enforce([changed("unrelated.cfg")])
    assert summary["flagged"] == ["unrelated.cfg"]
    assert summary["rolled_back"] == []
    assert (ws / "unrelated.cfg").read_text() == "x=2\n"


def test_untracked_out_of_scope_file_is_deleted(tmp_path):
    ws = make_repo(tmp_path)
    (ws / "scratch.tmp").write_text("junk\n", encoding="utf-8")
    guard = ScopeGuard(ws, ISSUE, rollback=True)
    summary = guard.enforce([changed("src/buggy.py"), changed("scratch.tmp", status="??")])
    assert summary["flagged"] == ["scratch.tmp"]
    assert not (ws / "scratch.tmp").exists()


def test_judge_flags_are_reported_but_not_rolled_back(tmp_path):
    ws = make_repo(tmp_path)
    (ws / "docs").mkdir()
    (ws / "docs" / "notes.md").write_text("changed\n", encoding="utf-8")

    class Judge:
        available = True
        max_state_chars = 1100
        task_excerpt = ""

        def judge_batch(self, states, questions):
            return [{"answers": {"in_scope": {"type": "noul", "p_true": 0.05, "label": "false"}}, "source": "laya"}]

    guard = ScopeGuard(ws, ISSUE, judge=Judge(), rollback=True)
    summary = guard.enforce([changed("docs/notes.md")])
    assert summary["flagged"] == ["docs/notes.md"]
    assert summary["rolled_back"] == []  # judge-only flag: reported, never reverted
    assert (ws / "docs" / "notes.md").read_text() == "changed\n"


def test_cache_paths_are_ignored(tmp_path):
    ws = make_repo(tmp_path)
    guard = ScopeGuard(ws, ISSUE)
    summary = guard.enforce([changed("__pycache__/x.pyc", status="??"), changed(".pytest_cache/v/cache", status="??")])
    assert summary["ignored"]
    assert summary["flagged"] == []


def test_telemetry_records_scope(tmp_path):
    from harness.telemetry import Telemetry

    ws = make_repo(tmp_path)
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    guard = ScopeGuard(ws, ISSUE, telemetry=telemetry)
    guard.enforce([changed("src/buggy.py"), changed("unrelated.cfg")])
    events = telemetry.events_of("scope_check")
    assert events and events[0]["flagged"] == ["unrelated.cfg"]
    telemetry.close()
