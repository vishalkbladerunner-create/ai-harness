"""End-to-end self-verification: failures are fed back, then green, then submitted."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.dryrun import toolcall
from harness.issue import parse_issue
from harness.run import RunOptions, execute_run


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "fixture"
    ws.mkdir()
    (ws / "buggy.py").write_text(
        "def last_item(items):\n    \"\"\"Return the last item in a list.\"\"\"\n    if not items:\n        return None\n    return items[len(items)]\n",
        encoding="utf-8",
    )
    (ws / "test_buggy.py").write_text(
        "from buggy import last_item\n\n\ndef test_last_item():\n"
        "    assert last_item([1, 2, 3]) == 3\n    assert last_item([\"a\"]) == \"a\"\n    assert last_item([]) is None\n",
        encoding="utf-8",
    )
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@e.invalid"], ["git", "config", "user.name", "t"],
                ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=ws, check=True)
    return ws


def observation_text(message: dict) -> str:
    """Observations are JSON-rendered by the upstream template; decode when possible."""
    content = message.get("content") or ""
    try:
        decoded = json.loads(content)
        if isinstance(decoded, dict) and isinstance(decoded.get("output"), str):
            return decoded["output"]
    except (json.JSONDecodeError, TypeError):
        pass
    return content


def test_verification_failure_is_fed_back_then_fixed(tmp_path):
    ws = make_workspace(tmp_path)
    scenario = [
        toolcall("cat buggy.py && cat test_buggy.py", "Read the bug and its test."),
        # deliberately wrong first attempt: the verification loop should catch it
        toolcall("sed -i.bak 's/items\\[len(items)\\]/items[0]/' buggy.py && rm -f buggy.py.bak", "First (wrong) attempt."),
        toolcall("sed -i.bak 's/items\\[0\\]/items[len(items) - 1]/' buggy.py && rm -f buggy.py.bak", "Correct the fix."),
        toolcall("harness_submit_patch", "Submit with test evidence."),
    ]
    result = execute_run(
        RunOptions(
            issue=parse_issue(
                "Fix buggy.py:last_item so test_buggy.py passes. It returns the wrong element for a non-empty list.\n",
                source="unit",
            ),
            workspace=ws,
            reports_root=tmp_path / "reports",
            dry_run=True,
            scenario=scenario,
        )
    )

    assert result.status == "submitted"
    trajectory = json.loads((result.run_dir / "trajectory.json").read_text())
    tool_messages = [m for m in trajectory["messages"] if m.get("role") == "tool"]
    fail_blocks = [m for m in tool_messages if '<verification status="FAIL"' in observation_text(m)]
    pass_evidence = [
        m for m in tool_messages
        if "Final verification: PASS" in observation_text(m) or '<verification status="PASS"' in observation_text(m)
    ]
    assert fail_blocks, "a failing verification must be fed back to the model"
    assert pass_evidence, "passing verification evidence must exist before submission"
    assert tool_messages.index(fail_blocks[0]) < tool_messages.index(pass_evidence[-1])

    # final state: the fixture tests really pass
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ws, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0

    # and the submission text carries the verification evidence
    assert "Final verification: PASS" in result.submission
    report = result.report_path.read_text(encoding="utf-8")
    assert "## Verification evidence" in report
    assert "1 passed" in report
