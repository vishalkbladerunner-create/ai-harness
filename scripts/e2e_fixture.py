#!/usr/bin/env python3
"""
Fixture E2E — run the real harness against tests/fixture-repo, mock or live.

Why it exists: the strongest available proof that the evaluation path works is a
copy of the fixture repo, an off-by-one issue, and an assertion that the test
suite passes afterwards. Mock mode uses the loopback OpenAI-compatible server
(no credentials, no network); live mode uses the evaluator's endpoint.

Hackathon criteria served: Phase 6.1 (`make test` assertion), Phase 6.2 (clean-
environment verification).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.issue import parse_issue  # noqa: E402
from harness.run import RunOptions, execute_run  # noqa: E402
from tests.mock_model_server import MockModelServer  # noqa: E402

ISSUE_TEXT = (
    "Fix the failing test in this repository.\n\n"
    "test_buggy.py fails because buggy.py:last_item returns the wrong element for a "
    "non-empty list. Make the test pass.\n"
)


def prepare_fixture(dest: Path) -> Path:
    source = REPO_ROOT / "tests" / "fixture-repo"
    shutil.copytree(source, dest, dirs_exist_ok=True)
    for pycache in dest.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "guarded-mini test"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture baseline"], cwd=dest, check=True)
    return dest


@contextmanager
def mock_endpoint():
    from harness.dryrun import fixture_scenario

    server = MockModelServer(fixture_scenario()).start()
    saved = {k: os.environ.get(k) for k in ("AI_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")}
    os.environ.update(
        {
            "AI_API_KEY": "mock-key-not-a-real-credential",
            "MODEL_BASE_URL": server.base_url,
            "MODEL_NAME": "mock-model",
        }
    )
    try:
        yield server
    finally:
        server.stop()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_pytest(workspace: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=workspace, capture_output=True, text=True, timeout=180
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]


def main() -> int:
    parser = argparse.ArgumentParser(description="run the harness against the fixture repo")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--budget", type=float, default=5.0, help="wall-clock minutes")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace")
    args = parser.parse_args()

    if args.mode == "live":
        missing = [k for k in ("AI_API_KEY", "MODEL_BASE_URL", "MODEL_NAME") if not os.environ.get(k)]
        if missing:
            print(f"[e2e] SKIP: live mode requires {', '.join(missing)}")
            return 2
    if not shutil.which("git"):
        print("[e2e] SKIP: git is required for the fixture workspace")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix=f"guarded-mini-e2e-{args.mode}-"))
    workspace = prepare_fixture(tmp / "fixture-repo")
    print(f"[e2e] mode={args.mode} workspace={workspace}")

    context = mock_endpoint() if args.mode == "mock" else nullcontext(None)
    with context as server:
        issue = parse_issue(ISSUE_TEXT, source="fixture")
        result = execute_run(
            RunOptions(issue=issue, workspace=workspace, budget_minutes=args.budget, max_steps=args.max_steps)
        )

    tests_ok, test_output = run_pytest(workspace)
    report_text = result.report_path.read_text(encoding="utf-8") if result.report_path and result.report_path.exists() else ""
    patch_text = result.patch_path.read_text(encoding="utf-8") if result.patch_path and result.patch_path.exists() else ""
    checks = {
        "agent submitted": result.status == "submitted",
        "fixture tests pass after run": tests_ok,
        "report written": "guarded-mini run report" in report_text,
        "patch captured": "buggy.py" in patch_text,
        "telemetry has model calls": bool(
            [line for line in result.telemetry_path.read_text(encoding="utf-8").splitlines() if '"model_call"' in line]
        ),
    }
    # Compliance: the runtime credential must never be written to any run artefact
    # (trajectory included). This is the regression check for that rule.
    credential = os.environ.get("AI_API_KEY", "")
    leaked: list[str] = []
    if credential:
        for artefact in sorted(result.run_dir.rglob("*")):
            if not artefact.is_file():
                continue
            try:
                if credential in artefact.read_text(encoding="utf-8", errors="ignore"):
                    leaked.append(str(artefact.relative_to(result.run_dir)))
            except OSError:
                continue
    checks["no credential material in run artefacts"] = not leaked
    if leaked:
        print(f"[e2e] CREDENTIAL LEAK in: {', '.join(leaked)}")
    print("\n[e2e] checks:")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n[e2e] pytest tail:\n{test_output}")
    if result.report_path:
        print(f"[e2e] report: {result.report_path}")
    if result.patch_path:
        print(f"[e2e] patch: {result.patch_path}")
    if args.keep:
        print(f"[e2e] kept workspace: {workspace}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)

    if all(checks.values()):
        print("[e2e] RESULT: PASS")
        return 0
    print("[e2e] RESULT: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
