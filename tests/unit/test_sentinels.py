"""Harness sentinels: load_issue and submit_patch (Phase 3.2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.issue import parse_issue
from harness.run import build_sentinels
from harness.verify import Verifier


def make_project(tmp_path: Path) -> Path:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "buggy.py").write_text("def last(items):\n    return items[len(items)]\n", encoding="utf-8")
    (ws / "test_buggy.py").write_text("from buggy import last\n\n\ndef test_last():\n    assert last([1, 2]) == 2\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@e.invalid"], ["git", "config", "user.name", "t"],
                ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=ws, check=True)
    return ws


def test_load_issue_sentinel_returns_issue_and_metadata(tmp_path):
    sentinels = build_sentinels(parse_issue("Fix buggy.py:last_item", source="unit"), tmp_path, tmp_path)
    output = sentinels["harness_load_issue"]("")["output"]
    assert "Fix buggy.py:last_item" in output
    assert "referenced paths" in output


def test_submit_patch_sentinel_emits_submission_and_evidence(tmp_path):
    ws = make_project(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (ws / "buggy.py").write_text("def last(items):\n    return items[len(items) - 1]\n", encoding="utf-8")
    verifier = Verifier(ws)
    sentinels = build_sentinels(parse_issue("fix the failing test", source="unit"), ws, run_dir,
                                verifier=verifier, patch_path=run_dir / "patch.diff")
    result = sentinels["harness_submit_patch"]("")
    first_line = result["output"].splitlines()[0]
    assert first_line == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    assert "buggy.py" in result["output"]
    assert "Final verification: PASS" in result["output"]
    assert (run_dir / "patch.diff").exists()
    assert "items[len(items) - 1]" in (run_dir / "patch.diff").read_text()
    assert verifier.runs == 1


def test_submit_patch_without_tests_says_so(tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    verifier = Verifier(ws)  # no test command detectable
    sentinels = build_sentinels(parse_issue("nothing to test", source="unit"), ws, run_dir,
                                verifier=verifier, patch_path=run_dir / "patch.diff")
    output = sentinels["harness_submit_patch"]("")["output"]
    assert "No test command could be detected" in output
