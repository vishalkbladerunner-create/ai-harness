"""Workspace guards + full dry-run lifecycle (no API calls)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.config import REPO_ROOT
from harness.dryrun import default_scenario, toolcall
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


def test_default_cwd_harness_repo_warns_but_returns(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    resolved = resolve_workspace(None, parse_issue("fix something", source="unit"))
    assert resolved == REPO_ROOT
    assert "warning" in capsys.readouterr().err.lower()


def test_absolute_git_dir_named_in_issue_is_used(tmp_path, monkeypatch):
    target = tmp_path / "target-repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    monkeypatch.chdir(REPO_ROOT)
    issue = parse_issue(f"Fix the failing test in {target}", source="unit")
    assert resolve_workspace(None, issue) == target.resolve()


def test_repo_url_is_cloned_when_no_workspace(tmp_path, monkeypatch):
    cloned = tmp_path / "cloned"
    cloned.mkdir()
    monkeypatch.setattr("harness.run._clone_workspace", lambda url: cloned)
    monkeypatch.chdir(REPO_ROOT)
    issue = parse_issue("Repo: acme/widgets\nFix the failing test", source="unit")
    assert resolve_workspace(None, issue) == cloned


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


class _FlaggingJudge:
    """Judge stub that flags every non-deterministic file as out of scope."""

    available = True
    source = "laya"
    task_excerpt = ""
    max_state_chars = 1100
    calibration: dict = {}

    def status(self) -> dict:
        return {"checkpoint": "stub", "available": True}

    def judge_batch(self, states, questions):
        results = []
        for _ in states:
            answers = {}
            for qid, question in questions.items():
                if question.get("type") == "noul":
                    answers[qid] = {"type": "noul", "p_true": 0.0, "raw_p_true": 0.0, "label": "false"}
                elif question.get("type") == "choice":
                    answers[qid] = {
                        "type": "choice",
                        "probabilities": {"safe": 1.0, "keep": 1.0, "shorten": 0.0, "drop": 0.0},
                        "raw_probabilities": {"safe": 1.0, "keep": 1.0, "shorten": 0.0, "drop": 0.0},
                    }
                else:
                    answers[qid] = {"type": "score", "score": 3.0, "probabilities": {"3": 1.0}}
            results.append({"answers": answers, "source": "laya"})
        return results


def test_judge_flagged_file_is_left_in_workspace_but_not_in_patch(tmp_path, monkeypatch):
    ws = make_git_workspace(tmp_path)
    scenario = [
        toolcall("printf '# fix\\n' >> app.py", "apply an in-scope change (anchors the scope check)"),
        toolcall("printf 'junk\\n' > junk.txt", "create an unrelated file"),
        toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "submit"),
    ]
    monkeypatch.setattr("harness.run.build_judge", lambda *args, **kwargs: _FlaggingJudge())
    result = execute_run(
        RunOptions(
            issue=parse_issue("fix app.py", source="unit"),
            workspace=ws,
            reports_root=tmp_path / "reports",
            dry_run=True,
            scenario=scenario,
        )
    )
    assert result.status == "submitted"
    # conservative: judge flags are reported, never rolled back in the workspace
    assert (ws / "junk.txt").exists()
    # ...but they must not ship inside the submitted patch
    patch = result.patch_path.read_text(encoding="utf-8")
    assert "junk.txt" not in patch
    assert "app.py" in patch
    report = result.report_path.read_text(encoding="utf-8")
    assert "Excluded from the submitted patch" in report
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    assert any(e["kind"] == "patch_exclusions" and "junk.txt" in (e.get("paths") or []) for e in events)


def test_without_anchor_judge_flags_are_report_only(tmp_path, monkeypatch):
    ws = make_git_workspace(tmp_path)
    scenario = [
        toolcall("printf 'junk\\n' > junk.txt", "create an unrelated file"),
        toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "submit"),
    ]
    monkeypatch.setattr("harness.run.build_judge", lambda *args, **kwargs: _FlaggingJudge())
    result = execute_run(
        RunOptions(
            issue=parse_issue("fix app.py", source="unit"),
            workspace=ws,
            reports_root=tmp_path / "reports",
            dry_run=True,
            scenario=scenario,
        )
    )
    # No deterministic in-scope change anchors the scope reading: nothing is
    # excluded (a wrong exclusion is worse than a reported oddity).
    assert "junk.txt" in result.patch_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    assert not any(e["kind"] == "patch_exclusions" for e in events)


def test_stall_detection_nudges_then_exits(tmp_path, monkeypatch):
    """The identical action repeated forever: nudge at the limit, stuck exit at 2x."""
    from harness.config import load_harness_config

    config = load_harness_config()
    config["budget"]["stall_duplicate_limit"] = 2
    monkeypatch.setattr("harness.run.load_harness_config", lambda: config)

    ws = make_git_workspace(tmp_path)
    scenario = [toolcall("true", "stuck loop") for _ in range(8)]
    result = execute_run(
        RunOptions(
            issue=parse_issue("fix app.py", source="unit"),
            workspace=ws,
            reports_root=tmp_path / "reports",
            dry_run=True,
            scenario=scenario,
        )
    )
    assert result.status == "limits_exceeded"
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    assert any(e["kind"] == "stall_warning" for e in events)
    assert any(e["kind"] == "harness_notice" for e in events), "the nudge must reach the model's context"
    assert any("stuck" in (e.get("reason") or "") for e in events if e["kind"] == "budget_exhausted")
    # the loop stopped long before the 8 scripted repetitions were consumed
    steps = [e for e in events if e["kind"] == "step"]
    assert len(steps) < 8


def test_varied_actions_never_trigger_the_stall_detector(tmp_path, monkeypatch):
    from harness.config import load_harness_config

    config = load_harness_config()
    config["budget"]["stall_duplicate_limit"] = 2
    monkeypatch.setattr("harness.run.load_harness_config", lambda: config)

    ws = make_git_workspace(tmp_path)
    result = execute_run(
        RunOptions(
            issue=parse_issue("Create a marker file", source="unit"),
            workspace=ws,
            reports_root=tmp_path / "reports",
            dry_run=True,
            scenario=default_scenario(),
        )
    )
    assert result.status == "submitted"
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    assert not any(e["kind"] == "stall_warning" for e in events)
