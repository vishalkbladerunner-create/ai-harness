#!/usr/bin/env python3
"""
One-command SWE-bench Verified run + score — `make bench INSTANCE=<id> [id...]`.

Per instance this script performs the full benchmark procedure, end to end:

  1. fetch the instance data from HuggingFace (datasets-server API),
  2. clone the repository (blob-filtered, cached) into .cache/benchmarks and
     create a clean worktree at the instance's exact base commit,
  3. run the harness headless on the verbatim problem statement
     (self-verification off — scoring runs the benchmark's own tests),
  4. score: a second fresh worktree + the benchmark's test_patch + the
     harness's patch.diff, then run every FAIL_TO_PASS test,
  5. print a results table (status, tests, files-vs-gold, tokens, est. cost)
     and append it to reports/bench-results.jsonl as evidence.

Requirements: AI_API_KEY exported (DeepSeek or Qwen), network for the dataset,
the clone, and the model. Python deps of the target repo must be importable by
this venv (for sympy that is just `pip install mpmath`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.issue import parse_issue  # noqa: E402
from harness.run import RunOptions, execute_run  # noqa: E402

HF_ROWS = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Verified&config=default&split=test&offset={offset}&length=100"
)
BENCH_CACHE = REPO_ROOT / ".cache" / "benchmarks"
RESULTS_FILE = REPO_ROOT / "reports" / "bench-results.jsonl"
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"


def fetch_instance(instance_id: str) -> dict:
    """Find one instance row on the datasets-server (paginated, 500 rows)."""
    for offset in range(0, 500, 100):
        with urllib.request.urlopen(HF_ROWS.format(offset=offset), timeout=60) as response:
            data = json.load(response)
        for row in data.get("rows", []):
            if row["row"]["instance_id"] == instance_id:
                return row["row"]
    raise SystemExit(f"instance not found in SWE-bench Verified: {instance_id}")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{proc.stderr[:400]}")
    return proc.stdout.strip()


def prepare_worktree(repo: str, base_commit: str, name: str) -> Path:
    """Cached blob-filtered clone + a pristine worktree at the base commit."""
    slug = repo.replace("/", "-")
    clone = BENCH_CACHE / slug
    clone.parent.mkdir(parents=True, exist_ok=True)
    if not (clone / ".git").exists():
        print(f"[bench] cloning {repo} (blob-filtered, cached)…")
        git("clone", "--quiet", "--filter=blob:none", f"https://github.com/{repo}", str(clone))
    worktree = BENCH_CACHE / name
    if worktree.exists():
        git("-C", str(clone), "worktree", "remove", "--force", str(worktree))
    git("-C", str(clone), "worktree", "add", "--quiet", "--force", str(worktree), base_commit)
    return worktree


def run_tests(worktree: Path, fail_to_pass: list[str], test_files: list[str]) -> dict:
    """Run every FAIL_TO_PASS test (-k by name, restricted to the patched files)."""
    python = str(VENV_PY) if VENV_PY.exists() else sys.executable
    results = {}
    for test_name in fail_to_pass:
        short = test_name.split("::")[-1]
        command = [python, "-m", "pytest", *test_files, "-k", short, "-q"]
        proc = subprocess.run(command, cwd=worktree, capture_output=True, text=True, timeout=600)
        results[test_name] = proc.returncode == 0
    return results


def cost_estimate(telemetry_path: Path) -> tuple[int, float]:
    """(total tokens, est. USD at DeepSeek peak prices with cache-hit rates)."""
    prompt = completion = cache = 0
    for line in telemetry_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("kind") != "model_call":
            continue
        usage = event.get("usage") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        cache += int(usage.get("cache_hit_tokens") or 0)
    cost = cache * 0.006 / 1e6 + (prompt - cache) * 0.3 / 1e6 + completion * 1.2 / 1e6
    return prompt + completion, cost


def bench_instance(instance_id: str, *, budget_minutes: float, max_steps: int | None) -> dict:
    instance = fetch_instance(instance_id)
    repo, base = instance["repo"], instance["base_commit"]
    print(f"[bench] {instance_id} — {repo} @ {base[:10]}")

    run_wt = prepare_worktree(repo, base, f"run-{instance_id}")
    score_wt = prepare_worktree(repo, base, f"score-{instance_id}")
    (score_wt / ".bench_test_patch").write_text(instance["test_patch"], encoding="utf-8")
    git("-C", str(score_wt), "apply", str(score_wt / ".bench_test_patch"))
    test_files = sorted(
        {
            line.split(" b/", 1)[1]
            for line in instance["test_patch"].splitlines()
            if line.startswith("diff --git") and line.rstrip().endswith(".py")
        }
    )

    issue = parse_issue(instance["problem_statement"], source=f"swe-bench:{instance_id}")
    print(f"[bench] running the harness on the problem statement ({len(issue.text)} chars)…")
    result = execute_run(
        RunOptions(
            issue=issue,
            workspace=run_wt,
            budget_minutes=budget_minutes,
            max_steps=max_steps,
            verify_mode="off",
        )
    )
    print(f"[bench] run finished: {result.status} (report: {result.report_path})")

    tests: dict = {}
    if result.status == "submitted" and result.patch_path and result.patch_path.exists():
        applied = subprocess.run(
            ["git", "-C", str(score_wt), "apply", str(result.patch_path)],
            capture_output=True, text=True, timeout=60,
        )
        if applied.returncode != 0:
            print(f"[bench] WARNING: our patch does not apply cleanly: {applied.stderr[:200]}")
        tests = run_tests(score_wt, json.loads(instance["FAIL_TO_PASS"]), test_files)
    gold_files = sorted(
        line.split(" b/", 1)[1] for line in instance["patch"].splitlines() if line.startswith("diff --git")
    )
    our_files = []
    if result.patch_path and result.patch_path.exists():
        our_files = sorted(
            line.split(" b/", 1)[1]
            for line in result.patch_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("diff --git")
        )
    tokens, cost = cost_estimate(result.telemetry_path)
    solved = bool(tests) and all(tests.values())
    record = {
        "instance_id": instance_id,
        "status": result.status,
        "solved": solved,
        "fail_to_pass": tests,
        "gold_files": gold_files,
        "our_files": our_files,
        "files_match_gold": sorted(our_files) == gold_files,
        "tokens": tokens,
        "cost_usd_est": round(cost, 4),
        "run_dir": str(result.run_dir),
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def main(argv: list[str]) -> int:
    args = list(argv)
    budget = 15.0
    max_steps = None
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--budget":
            budget = float(args.pop(0))
        elif flag == "--max-steps":
            max_steps = int(args.pop(0))
    if not args:
        print(__doc__)
        return 2
    if not os.environ.get("AI_API_KEY"):
        print("error: export AI_API_KEY first (the official DeepSeek or Qwen key).")
        return 2
    # The agent's shell `python` must be this venv (repo deps like mpmath live here).
    os.environ["PATH"] = f"{VENV_PY.parent}:{os.environ['PATH']}" if VENV_PY.exists() else os.environ["PATH"]

    records = [bench_instance(iid, budget_minutes=budget, max_steps=max_steps) for iid in args]
    print("\n================= SWE-bench Verified — results =================")
    solved = 0
    for record in records:
        mark = "SOLVED" if record["solved"] else record["status"]
        solved += bool(record["solved"])
        tests = ", ".join(f"{name.split('::')[-1]}={'PASS' if ok else 'FAIL'}" for name, ok in record["fail_to_pass"].items()) or "—"
        print(f"  {record['instance_id']:32} {mark:16} {tests}")
        print(f"  {'':32} files={'gold' if record['files_match_gold'] else record['our_files']} "
              f"tokens={record['tokens']:,} est_cost=${record['cost_usd_est']:.3f}")
    print(f"  {'-' * 70}\n  solved {solved}/{len(records)}   (details: {RESULTS_FILE})")
    return 0 if solved == len(records) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
