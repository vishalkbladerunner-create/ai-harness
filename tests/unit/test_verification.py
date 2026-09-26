"""Self-verification loop: change detection, evidence blocks, dedup (Phase 3.1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.verify import VerificationLoop, Verifier


def make_project(tmp_path: Path) -> Path:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=ws, check=True)
    return ws


def test_auto_mode_runs_after_a_file_changes(tmp_path):
    ws = make_project(tmp_path)
    verifier = Verifier(ws)
    loop = VerificationLoop(verifier, mode="auto")
    assert loop.postprocessor("ls -la", {"returncode": 0}, {}) == []
    (ws / "new_file.py").write_text("x = 1\n", encoding="utf-8")
    blocks = loop.postprocessor("printf 'x = 1\\n' > new_file.py", {"returncode": 0}, {})
    assert blocks and "<verification status=\"PASS\"" in blocks[0]
    assert verifier.runs == 1


def test_auto_mode_does_not_rerun_without_changes(tmp_path):
    ws = make_project(tmp_path)
    verifier = Verifier(ws)
    loop = VerificationLoop(verifier, mode="auto")
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert loop.postprocessor("touch a.py", {"returncode": 0}, {})
    assert loop.postprocessor("cat a.py", {"returncode": 0}, {}) == []
    assert verifier.runs == 1


def test_failing_tests_are_reported_as_fail_blocks(tmp_path):
    ws = make_project(tmp_path)
    verifier = Verifier(ws)
    loop = VerificationLoop(verifier, mode="auto")
    (ws / "test_ok.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")
    blocks = loop.postprocessor("edit test", {"returncode": 0}, {})
    assert blocks and 'status="FAIL"' in blocks[0]
    assert "FAILED" in blocks[0] or "failed" in blocks[0]


def test_off_mode_never_runs(tmp_path):
    ws = make_project(tmp_path)
    verifier = Verifier(ws)
    loop = VerificationLoop(verifier, mode="off")
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert loop.postprocessor("touch a.py", {"returncode": 0}, {}) == []
    assert verifier.runs == 0


def test_on_mode_runs_every_command(tmp_path):
    ws = make_project(tmp_path)
    verifier = Verifier(ws)
    loop = VerificationLoop(verifier, mode="on")
    assert loop.postprocessor("ls", {"returncode": 0}, {})
    assert loop.postprocessor("ls", {"returncode": 0}, {})
    assert verifier.runs == 2


def test_model_test_run_is_not_duplicated(tmp_path):
    ws = make_project(tmp_path)
    verifier = Verifier(ws)
    loop = VerificationLoop(verifier, mode="auto")
    blocks = loop.postprocessor("python -m pytest -q", {"returncode": 0}, {})
    assert blocks == []
    assert verifier.runs == 0
    assert loop.skipped_as_duplicate == 1


def test_verification_evidence_reaches_report_context(tmp_path):
    from harness.telemetry import Telemetry

    ws = make_project(tmp_path)
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    verifier = Verifier(ws, telemetry=telemetry)
    loop = VerificationLoop(verifier, mode="auto", telemetry=telemetry)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    loop.postprocessor("touch a.py", {"returncode": 0}, {})
    events = telemetry.events_of("verification")
    assert events and events[0]["ok"] is True and events[0]["reason"].startswith("after command")
    telemetry.close()
