#!/usr/bin/env python3
"""
Compaction study — shadow mode vs live mode on the fixture repo (Phase 2.6).

Why it exists: thresholds must come from evidence. This script runs the same
verbose fixture conversation twice (compaction shadow -> live) with the local
judge, records verdict counts / tokens / fixture-test outcomes, and writes
reports/compaction-study.md for the README calibration appendix.

No API credentials required (mock model endpoint + local laya).

Usage:
    .venv/bin/python scripts/compaction_study.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.dryrun import verbose_fixture_scenario  # noqa: E402
from harness.issue import parse_issue  # noqa: E402
from harness.run import RunOptions, execute_run  # noqa: E402
from tests.mock_model_server import MockModelServer  # noqa: E402

ISSUE_TEXT = (
    "Fix the failing test in this repository.\n\n"
    "test_buggy.py fails because buggy.py:last_item returns the wrong element for a "
    "non-empty list. Make the test pass.\n"
)


def prepare_fixture(dest: Path) -> Path:
    shutil.copytree(REPO_ROOT / "tests" / "fixture-repo", dest, dirs_exist_ok=True)
    for pycache in dest.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "h@e.invalid"],
                ["git", "config", "user.name", "h"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=dest, check=True)
    return dest


def run_once(mode: str, reports_root: Path) -> dict:
    import harness.config as config_module
    import harness.run as run_module

    original_harness = run_module.load_harness_config
    original_compaction = run_module.load_compaction_config

    def harness_config() -> dict:
        config = original_harness()
        config.setdefault("compaction", {})["mode"] = mode
        return config

    def compaction_config() -> dict:
        config = original_compaction()
        # The study deliberately uses a small target so the fixture conversation
        # crosses it; production thresholds stay at the checked-in defaults.
        config.setdefault("compaction", {}).update({"mode": mode, "target_tokens": 1200, "recent_tokens": 400})
        return config

    tmp = Path(tempfile.mkdtemp(prefix=f"compaction-{mode}-"))
    workspace = prepare_fixture(tmp / "fixture-repo")
    server = MockModelServer(verbose_fixture_scenario()).start()
    import os

    saved = {k: os.environ.get(k) for k in ("AI_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")}
    os.environ.update({"AI_API_KEY": "mock-key-not-real", "MODEL_BASE_URL": server.base_url, "MODEL_NAME": "mock-model"})
    run_module.load_harness_config = harness_config  # type: ignore[assignment]
    run_module.load_compaction_config = compaction_config  # type: ignore[assignment]
    try:
        result = execute_run(
            RunOptions(issue=parse_issue(ISSUE_TEXT, source="study"), workspace=workspace,
                       reports_root=reports_root, budget_minutes=3, max_steps=25)
        )
    finally:
        run_module.load_harness_config = original_harness  # type: ignore[assignment]
        server.stop()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=workspace, capture_output=True, text=True, timeout=120)
    events = [json.loads(line) for line in result.telemetry_path.read_text().splitlines() if line.strip()]
    compaction_events = [e for e in events if e["kind"] == "compaction"]
    verdicts = [v for e in compaction_events for v in (e.get("verdicts") or [])]
    dropped = sorted({i for e in compaction_events for i in (e.get("dropped") or [])})
    shortened = sorted({i for e in compaction_events for i in (e.get("shortened") or [])})
    summary = {
        "mode": mode,
        "status": result.status,
        "fixture_tests_pass": proc.returncode == 0,
        "compaction_runs": len(compaction_events),
        "items_judged": len(verdicts),
        "verdict_counts": {
            "keep": sum(1 for v in verdicts if v["verdict"] == "keep"),
            "drop": sum(1 for v in verdicts if v["verdict"] == "drop"),
            "shorten": sum(1 for v in verdicts if v["verdict"] == "shorten"),
        },
        "judge_source": compaction_events[-1].get("judge_source") if compaction_events else None,
        "tokens_before": compaction_events[-1].get("tokens_before") if compaction_events else 0,
        "tokens_after": compaction_events[-1].get("tokens_after") if compaction_events else 0,
        "tokens_saved": sum(e.get("tokens_saved", 0) for e in compaction_events),
        "dropped_items": dropped,
        "shortened_items": shortened,
        "pinned_items": compaction_events[-1].get("pinned") if compaction_events else [],
        "recent_items": compaction_events[-1].get("recent") if compaction_events else [],
        "degradations": [e.get("reason") for e in events if e["kind"] == "degradation"],
        "report": str(result.report_path),
        "run_dir": str(result.run_dir),
    }
    shutil.rmtree(tmp, ignore_errors=True)
    return summary


def main() -> int:
    reports_root = REPO_ROOT / "reports" / "compaction-study"
    results = [run_once("shadow", reports_root), run_once("live", reports_root)]
    lines = [
        "# Compaction study — shadow vs live (Phase 2.6)",
        "",
        "Same verbose fixture conversation, same local judge, same thresholds; only the mode differs.",
        "Generated by `scripts/compaction_study.py` (no API credentials used).",
        "",
        "| metric | shadow | live |",
        "|---|---|---|",
    ]
    keys = ["status", "fixture_tests_pass", "compaction_runs", "items_judged", "judge_source",
            "tokens_before", "tokens_after", "tokens_saved"]
    lines.append("Settings for this study: `target_tokens=1200`, `recent_tokens=400` (production: 60000/12000).")
    lines.append("")
    for key in keys:
        lines.append(f"| {key} | {results[0][key]} | {results[1][key]} |")
    lines.append(f"| verdict counts | {results[0]['verdict_counts']} | {results[1]['verdict_counts']} |")
    lines.append(f"| dropped items | {results[0]['dropped_items']} | {results[1]['dropped_items']} |")
    lines.append(f"| shortened items | {results[0]['shortened_items']} | {results[1]['shortened_items']} |")
    lines.append(f"| pinned (never judged) | {results[0]['pinned_items']} | {results[1]['pinned_items']} |")
    lines.append(f"| recency-exempt | {results[0]['recent_items']} | {results[1]['recent_items']} |")
    lines.append(f"| degradations | {results[0]['degradations']} | {results[1]['degradations']} |")
    lines.append("")
    lines.append(f"Shadow run report: `{results[0]['report']}`")
    lines.append(f"Live run report: `{results[1]['report']}`")
    lines.append("")
    out = reports_root / "compaction-study.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    ok = results[0]["fixture_tests_pass"] and results[1]["fixture_tests_pass"] and results[1]["status"] == "submitted"
    print(f"\n[study] {'PASS' if ok else 'FAIL'} — study written to {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
