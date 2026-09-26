#!/usr/bin/env python3
"""
Live smoke test — the Phase 0.4 proof: "create hello.txt containing done".

Why it exists: before any other feature, the harness must demonstrably run
end-to-end against the *live* evaluator endpoint (AI_API_KEY / MODEL_BASE_URL /
MODEL_NAME) on a trivial task. Run it with `make smoke`.

Hackathon criteria served: Phase 0.4 (prove end-to-end with a trivial task),
Phase 6.2 (clean-environment verification step).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.issue import parse_issue  # noqa: E402
from harness.run import RunOptions, execute_run  # noqa: E402

ISSUE_TEXT = (
    "Create a file named hello.txt in the repository root. It must contain exactly the word "
    "done and nothing else. Then submit your work."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="live smoke test: create hello.txt containing done")
    parser.add_argument("--budget", type=float, default=3.0, help="wall-clock minutes")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    missing = [k for k in ("AI_API_KEY", "MODEL_BASE_URL", "MODEL_NAME") if not os.environ.get(k)]
    if missing:
        print(f"[smoke] CANNOT RUN: missing {', '.join(missing)} in the environment.")
        print("[smoke] Export them (values are never written to disk) and retry: make smoke")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="guarded-mini-smoke-"))
    workspace = tmp / "smoke-repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=False)

    print(f"[smoke] workspace: {workspace}")
    print(f"[smoke] model: {os.environ['MODEL_NAME']} via {os.environ['MODEL_BASE_URL']}")
    issue = parse_issue(ISSUE_TEXT, source="smoke")
    result = execute_run(RunOptions(issue=issue, workspace=workspace, budget_minutes=args.budget, max_steps=args.max_steps))

    target = workspace / "hello.txt"
    content = target.read_text(encoding="utf-8").strip() if target.exists() else ""
    checks = {
        "agent submitted": result.status == "submitted",
        "hello.txt exists": target.exists(),
        "hello.txt contains done": content == "done",
        "report written": bool(result.report_path and result.report_path.exists()),
    }
    print("\n[smoke] checks:")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if result.report_path:
        print(f"[smoke] report: {result.report_path}")
    if args.keep:
        print(f"[smoke] kept workspace: {workspace}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(checks.values())
    print(f"[smoke] RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
