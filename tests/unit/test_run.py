"""Workspace guards + full dry-run lifecycle (no API calls)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.config import REPO_ROOT
from harness.dryrun import default_scenario
from harness.issue import parse_issue
from harness.run import RunOptions, UsageError, execute_run, resolve_workspace


def test_refuses_to_operate_on_harness_repo():
    with pytest.raises(UsageError) as excinfo:
        resolve_workspace(str(REPO_ROOT), parse_issue("fix it"))
    assert "refusing" in str(excinfo.value).lower()


def test_accepts_explicit_workspace(tmp_path):
    assert resolve_workspace(str(tmp_path), parse_issue("fix it")) == tmp_path.resolve()


def test_uses_issue_workspace_hint(tmp_path):
    issue = parse_issue(f"Workspace: {tmp_path}\nfix it")
    assert resolve_workspace(None, issue) == tmp_path.resolve()


def test_rejects_missing_workspace(tmp_path):
    with pytest.raises(UsageError):
        resolve_workspace(str(tmp_path / "nope"), parse_issue("fix"))


def make_git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=ws, check=True)
    return ws


def test_dry_run_lifecycle(tmp_path):
    ws = make_git_workspace(tmp_path)
    reports = tmp_path / "reports"
    issue = parse_issue("Create a marker file", source="unit")
    result = execute_run(
        RunOptions(issue=issue, workspace=ws, reports_root=reports, dry_run=True, scenario=default_scenario())
    )
    assert result.status == "submitted"
    assert (ws / "harness_dry_run_marker.txt").read_text().strip() == "done"
    assert result.report_path and result.report_path.exists()
    assert "guarded-mini run report" in result.report_path.read_text(encoding="utf-8")
    assert result.patch_path and "harness_dry_run_marker.txt" in result.patch_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    kinds = {e["kind"] for e in events}
    assert {"run_start", "model_call", "command", "run_end"} - kinds == set()
    assert all(e["kind"] for e in events), "every telemetry event must carry a kind"
    status = json.loads((result.run_dir / "status.json").read_text())
    assert status["status"] == "submitted"


def test_dry_run_verification_evidence(tmp_path):
    ws = tmp_path / "fixture"
    ws.mkdir()
    (ws / "buggy.py").write_text("def last(items):\n    return items[len(items)]\n", encoding="utf-8")
    (ws / "test_buggy.py").write_text(
        "from buggy import last\n\n\ndef test_last():\n    assert last([1, 2]) == 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    from harness.dryrun import fixture_scenario

    reports = tmp_path / "reports"
    result = execute_run(
        RunOptions(issue=parse_issue("fix the failing test", source="unit"), workspace=ws, reports_root=reports, dry_run=True, scenario=fixture_scenario())
    )
    assert result.status == "submitted"
    report = result.report_path.read_text(encoding="utf-8")
    assert "Verification evidence" in report
    assert "1 passed" in report
