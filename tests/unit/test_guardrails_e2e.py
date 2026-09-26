"""End-to-end guardrail behaviour inside a real (dry-run) agent loop."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.dryrun import toolcall
from harness.issue import parse_issue
from harness.run import RunOptions, execute_run


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=not-a-real-secret\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@e.invalid"], ["git", "config", "user.name", "t"],
                ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=ws, check=True)
    return ws


def test_forbidden_commands_are_refused_and_run_continues(tmp_path):
    ws = make_workspace(tmp_path)
    forbidden_dir = tmp_path / "forbidden-target"
    scenario = [
        toolcall("ls -la", "look around"),
        toolcall(f"rm -rf {forbidden_dir}", "try to delete something outside the workspace"),
        toolcall("cat .env", "try to read credentials"),
        toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "submit"),
    ]
    result = execute_run(
        RunOptions(
            issue=parse_issue("Fix app.py so that it prints hello", source="unit"),
            workspace=ws,
            reports_root=tmp_path / "reports",
            dry_run=True,
            scenario=scenario,
        )
    )

    # the run still finished through the graceful deny path
    assert result.status == "submitted"

    # nothing outside the workspace was touched, and .env was never read
    assert not forbidden_dir.exists()

    # refusals are recorded with reasons and reach the model as observations
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    guardrail = [e for e in events if e["kind"] == "guardrail"]
    refused = [e for e in guardrail if e["outcome"] == "refuse"]
    assert len(refused) == 2, guardrail
    assert {e["rule"] for e in refused} >= {"outside_workspace_write", "credential_access"}

    trajectory = json.loads(result.patch_path.parent.joinpath("trajectory.json").read_text())
    observations = [m for m in trajectory["messages"] if m.get("role") == "tool"]
    refused_observations = [m for m in observations if "POLICY REFUSAL" in (m.get("content") or "")]
    assert len(refused_observations) == 2
    assert all("Suggested safe alternative" in m["content"] for m in refused_observations)

    report = result.report_path.read_text(encoding="utf-8")
    assert "## Guardrail log" in report
    assert "credential_access" in report or "credential" in report.lower()


def test_shadow_mode_blocks_nothing(tmp_path):
    ws = make_workspace(tmp_path)
    scenario = [
        toolcall("cat .env", "read credentials in shadow mode"),
        toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "submit"),
    ]
    # shadow mode is a policy.yaml field; patch it for this run only
    import harness.run as run_module

    original = run_module.load_policy_config

    def shadow_config() -> dict:
        config = original()
        config["guardrails"]["mode"] = "shadow"
        return config

    run_module.load_policy_config = shadow_config  # type: ignore[assignment]
    try:
        result = execute_run(
            RunOptions(
                issue=parse_issue("Read the env file", source="unit"),
                workspace=ws,
                reports_root=tmp_path / "reports",
                dry_run=True,
                scenario=scenario,
            )
        )
    finally:
        run_module.load_policy_config = original  # type: ignore[assignment]

    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    refusal_events = [e for e in events if e["kind"] == "guardrail" and e["outcome"] == "refuse"]
    # In shadow mode the event still records the refusal, but the command runs.
    assert refusal_events and refusal_events[0]["mode"] == "shadow"
    assert result.status == "submitted"
