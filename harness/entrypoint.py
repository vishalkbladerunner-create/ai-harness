#!/usr/bin/env python3
"""
guarded-mini entrypoint — the process `make run` launches.

Why it exists: the evaluator's workflow is git clone -> export creds -> make setup
-> make run -> feed an issue. This file is the contract: it reads the task from
stdin or a file, reads credentials from the environment only, runs the harness
unattended, prints the result and writes reports/. It never prompts.

Hackathon criteria served: compliance 1/2 (make run launches OUR entrypoint),
Phase 0.3 (accept the evaluation task, run end-to-end, write the run report),
Phase 5.3 (CLI flags --issue/--verify/--report/--budget).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make `python harness/entrypoint.py` work exactly like `python -m harness.entrypoint`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import HARNESS_NAME, __version__  # noqa: E402
from harness.banner import print_banner  # noqa: E402
from harness.config import ConfigError, REPO_ROOT as CONFIG_REPO_ROOT  # noqa: E402
from harness.issue import load_issue  # noqa: E402
from harness.run import RunOptions, UsageError, execute_run, resolve_workspace  # noqa: E402

EXIT_OK = 0
EXIT_AGENT_FAILED = 1
EXIT_USAGE = 2
EXIT_BUDGET = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness.entrypoint",
        description=f"{HARNESS_NAME} — guarded SWE harness on mini-swe-agent. "
        "Reads an issue from stdin (or --issue FILE) and runs unattended.",
    )
    parser.add_argument("issue_path", nargs="?", default=None, help="path to the issue/test description file")
    parser.add_argument("--issue", dest="issue_file", default=None, help="path to the issue/test description file")
    parser.add_argument("--workspace", default=None, help="target repository (default: $HARNESS_WORKSPACE or cwd)")
    parser.add_argument("--budget", type=float, default=None, help="wall-clock budget in MINUTES (default: config)")
    parser.add_argument("--max-steps", type=int, default=None, help="hard step budget (default: config)")
    parser.add_argument("--verify", choices=["auto", "on", "off"], default="auto", help="self-verification mode")
    parser.add_argument("--report", default=None, help="override report output path")
    parser.add_argument("--dry-run", action="store_true", help="run a scripted scenario, no API calls")
    parser.add_argument("--scenario", default=None, help="JSON scenario for --dry-run (list of tool-call outputs)")
    parser.add_argument("--force", action="store_true", help="allow operating on the harness repo itself")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary as the last line")
    parser.add_argument("--quiet", action="store_true", help="suppress the banner")
    parser.add_argument("--verbose", action="store_true", help="verbose logging on stderr")
    parser.add_argument("--version", action="version", version=f"{HARNESS_NAME} {__version__}")
    return parser


def _read_stdin() -> str | None:
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        return sys.stdin.read()
    except Exception:
        return None


def _load_scenario(path: str | None) -> list[dict] | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "outputs" in data:
        data = data["outputs"]
    if not isinstance(data, list):
        raise UsageError("scenario file must contain a JSON list of tool-call outputs (or {'outputs': [...]})")
    return data


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.quiet:
        print_banner()

    # Task: explicit file wins, then positional path, then stdin.
    issue_arg = args.issue_file or args.issue_path
    try:
        issue = load_issue(issue_arg, stdin_text=None if issue_arg else _read_stdin())
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("usage: make run < issue.md   |   make run ISSUE=issue.md", file=sys.stderr)
        return EXIT_USAGE

    import os

    workspace_arg = args.workspace or os.environ.get("HARNESS_WORKSPACE") or None
    try:
        options = RunOptions(
            issue=issue,
            workspace=resolve_workspace(workspace_arg, issue, force=args.force),
            budget_minutes=args.budget,
            max_steps=args.max_steps,
            verify_mode=args.verify,
            dry_run=args.dry_run,
            scenario=_load_scenario(args.scenario),
            force=args.force,
            quiet=args.quiet,
        )
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"[{HARNESS_NAME}] issue: {len(issue.text)} chars from {issue.source}")
    print(f"[{HARNESS_NAME}] workspace: {options.workspace}")
    model_name = os.environ.get("MODEL_NAME", "")
    if model_name and not any(provider in model_name.lower() for provider in ("deepseek", "qwen")):
        # Non-fatal: the evaluation path prescribes DeepSeek/Qwen, but any
        # OpenAI-compatible model may be used for local testing.
        print(
            f"[{HARNESS_NAME}] note: MODEL_NAME={model_name!r} is not a DeepSeek/Qwen model; "
            "the evaluation path prescribes those endpoints.",
            file=sys.stderr,
        )
    print(f"[{HARNESS_NAME}] running...\n")

    try:
        result = execute_run(options)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.report:
        import shutil

        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if result.report_path and result.report_path.exists():
            shutil.copyfile(result.report_path, target)
            result.report_path = target

    stats = result.stats
    print(f"\n[{HARNESS_NAME}] status: {result.status} ({result.exit_status or 'no exit status'})")
    print(f"[{HARNESS_NAME}] steps: {stats.get('steps', 0)} | model calls: {stats.get('llm_calls', 0)} | "
          f"tokens: {stats.get('total_tokens', 0)} | wall: {stats.get('wall_seconds', 0)}s")
    if result.patch_path:
        print(f"[{HARNESS_NAME}] patch: {result.patch_path}")
    if result.report_path:
        print(f"[{HARNESS_NAME}] report: {result.report_path}")
    if result.error:
        print(f"[{HARNESS_NAME}] error: {result.error}", file=sys.stderr)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))

    if result.status == "submitted":
        return EXIT_OK
    if result.status == "limits_exceeded":
        return EXIT_BUDGET
    return EXIT_AGENT_FAILED


if __name__ == "__main__":
    sys.exit(main())
