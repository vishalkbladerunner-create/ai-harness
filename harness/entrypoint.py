#!/usr/bin/env python3
"""
guarded-mini entrypoint — the process `make run` launches.

Why it exists: the evaluator's workflow is git clone -> export creds -> make setup
-> make run -> feed an issue. This file is the contract: it reads the task from
stdin or a file (on an interactive terminal the TUI collects it in an input
field), reads credentials from the environment only, runs the harness
unattended, prints the result and writes reports/. It never prompts on the
piped evaluation path.

Hackathon criteria served: compliance 1/2 (make run launches OUR entrypoint),
Phase 0.3 (accept the evaluation task, run end-to-end, write the run report),
Phase 5.3 (CLI flags --issue/--verify/--report/--budget).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Make `python harness/entrypoint.py` work exactly like `python -m harness.entrypoint`.
# Script mode puts this file's own directory (harness/) on sys.path[0], where it
# shadows stdlib/third-party top-level modules (`import secrets` would resolve to
# harness/secrets.py, breaking litellm). Drop it and put the repo root first.
REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR in sys.path:
    sys.path.remove(_SCRIPT_DIR)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import HARNESS_NAME, __version__  # noqa: E402
from harness.banner import print_banner  # noqa: E402
from harness.config import ConfigError, REPO_ROOT as CONFIG_REPO_ROOT  # noqa: E402
from harness.issue import load_issue  # noqa: E402
from harness.run import GuidanceError, RunOptions, UsageError, execute_run, resolve_workspace  # noqa: E402

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
    parser.add_argument(
        "--tui",
        action="store_true",
        help="launch the live terminal UI (Textual): runs the normal headless entrypoint as a child "
        "process and renders its telemetry stream (graph + context panels). Default: on an "
        "interactive terminal; piped/scripted runs stay headless.",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="force the headless path even on an interactive terminal",
    )
    parser.add_argument(
        "--replay",
        default=None,
        help="with --tui: open an existing run (run dir, telemetry.jsonl, or reports/LATEST) read-only",
    )
    parser.add_argument("--verbose", action="store_true", help="verbose logging on stderr")
    parser.add_argument("--version", action="version", version=f"{HARNESS_NAME} {__version__}")
    return parser


def _read_stdin() -> str | None:
    """Read the task from stdin. Piped input is read directly; a TTY is prompted.

    The evaluator "feeds a GitHub issue/test case to the running harness": that is
    a pipe (`make run < issue.md`) on the scripted path. The interactive paste
    prompt here is only the fallback for terminals where the TUI cannot start —
    normally the TUI collects the task in its own input field instead.
    """
    if sys.stdin is None:
        return None
    if sys.stdin.isatty():
        try:
            print(f"[{HARNESS_NAME}] paste the issue/test text, then press Ctrl-D to run:", file=sys.stderr)
            data = sys.stdin.read()
            return data or None
        except KeyboardInterrupt:
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


# ---------------------------------------------------------------------------
# TUI launch (--tui / --replay). The TUI is a viewer: in live mode it launches
# the *same* headless entrypoint as a child process and tails its telemetry, so
# the evaluation path and the visual path cannot drift apart.
# ---------------------------------------------------------------------------
def _import_run_tui(soft: bool = False):
    """Import the TUI lazily; a missing optional dependency must be a message."""
    try:
        from harness.tui import run_tui

        return run_tui
    except ImportError as exc:
        if soft:
            print(
                f"[{HARNESS_NAME}] note: the TUI needs optional dependencies ({exc}); "
                "continuing headless. Run 'make setup' to install them.",
                file=sys.stderr,
            )
        else:
            print(
                f"error: the TUI needs the optional dependencies ({exc}). "
                "Run 'make setup' (installs textual + tiktoken) or "
                "'pip install \"textual>=1\" tiktoken'. The headless path (make run) is unaffected.",
                file=sys.stderr,
            )
        return None


def _tui_child_command(args, options: RunOptions, issue) -> list[str]:
    """The headless entrypoint invocation the TUI wraps, with the issue staged."""
    import time

    staging = CONFIG_REPO_ROOT / ".cache" / "tui"
    staging.mkdir(parents=True, exist_ok=True)
    issue_file = staging / f"issue-{time.strftime('%Y%m%d-%H%M%S')}.md"
    issue_file.write_text(issue.text, encoding="utf-8")

    command = [
        sys.executable,
        "-m",
        "harness.entrypoint",
        "--issue",
        str(issue_file),
        "--workspace",
        str(options.workspace),
        "--verify",
        options.verify_mode,
        "--quiet",
        "--json",
    ]
    if options.budget_minutes is not None:
        command += ["--budget", str(options.budget_minutes)]
    if options.max_steps is not None:
        command += ["--max-steps", str(options.max_steps)]
    if options.dry_run:
        command += ["--dry-run"]
    if args.scenario:
        command += ["--scenario", str(Path(args.scenario).expanduser().resolve())]
    if options.force:
        command += ["--force"]
    if args.report:
        command += ["--report", str(Path(args.report).expanduser().resolve())]
    return command


def _launch_tui(args, options: RunOptions | None, issue) -> int | None:
    """Run the TUI; returns None when it could not start (caller may go headless)."""
    import os

    run_tui = _import_run_tui(soft=not args.tui)
    if run_tui is None:
        return None
    reports_root = options.reports_root if options is not None else CONFIG_REPO_ROOT / "reports"
    if options is None:  # --replay: no run, no issue, no credentials needed
        return run_tui(reports_root=reports_root, replay=Path(args.replay))
    print(f"[{HARNESS_NAME}] TUI: the headless run continues in a child process; press q to quit.")
    # The child must import the same `harness` package regardless of cwd or
    # whether `pip install -e .` was run: put the repo root on its PYTHONPATH.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(CONFIG_REPO_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return run_tui(
        reports_root=reports_root,
        workspace=options.workspace,
        child_argv=_tui_child_command(args, options, issue),
        cwd=Path.cwd(),
        env=env,
    )


CONNECT_API_MESSAGE = (
    "[bold]Connect the API first.[/bold] Run this with your key, then submit again:\n"
    "  [bold]export AI_API_KEY=<your-key>[/bold]\n"
    "[dim]Only the official DeepSeek and Qwen APIs are supported right now — the harness "
    "detects which one your key belongs to automatically.[/dim]"
)


def _make_collect(args, workspace_arg: str | None):
    """Build the TUI collect-mode callback (importable for tests).

    The callback runs inside the TUI when the user submits the issue: parse the
    task, verify the credential is present (dry-runs need none), resolve the
    workspace and stage the child command. Any exception is shown on the input
    screen, never as a traceback.
    """

    def collect(issue_text: str) -> list[str]:
        issue = load_issue(None, stdin_text=issue_text)
        if not args.dry_run and not os.environ.get("AI_API_KEY"):
            raise UsageError(CONNECT_API_MESSAGE)
        options = RunOptions(
            issue=issue,
            workspace=resolve_workspace(workspace_arg, issue, force=args.force),
            budget_minutes=args.budget,
            max_steps=args.max_steps,
            verify_mode=args.verify,
            dry_run=args.dry_run,
            scenario=_load_scenario(args.scenario),
            force=args.force,
            quiet=True,
        )
        return _tui_child_command(args, options, issue)

    return collect


def _launch_tui_collect(args, workspace_arg: str | None) -> int | None:
    """Interactive `make run` with no issue file: the TUI collects the task.

    Returns the run's exit code, or None when the TUI cannot start (the caller
    then falls back to the classic stdin prompt). The issue-dependent work —
    parsing, workspace resolution, staging — happens inside the TUI via the
    ``collect`` callback, so the input screen can show errors and stay alive.
    """
    import os

    run_tui = _import_run_tui(soft=not args.tui)
    if run_tui is None:
        return None

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(CONFIG_REPO_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return run_tui(
        reports_root=CONFIG_REPO_ROOT / "reports",
        collect_issue=_make_collect(args, workspace_arg),
        cwd=Path.cwd(),
        env=env,
    )


def _tui_mode(args) -> str:
    """Resolve --tui/--no-tui into on / off / auto (default: auto)."""
    if args.tui and args.no_tui:
        raise ValueError("--tui and --no-tui are mutually exclusive")
    if args.tui:
        return "on"
    if args.no_tui:
        return "off"
    return "auto"


def _should_use_tui(mode: str) -> bool:
    """auto -> the TUI when attached to a real terminal; piped runs stay headless."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    try:
        import os

        return bool(
            sys.stdin is not None
            and sys.stdout is not None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
            and os.environ.get("TERM", "") not in ("", "dumb")
        )
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        mode = _tui_mode(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.replay:
        if mode != "on":
            print("error: --replay requires --tui", file=sys.stderr)
            return EXIT_USAGE
        code = _launch_tui(args, None, None)
        return code if code is not None else EXIT_USAGE

    use_tui = _should_use_tui(mode)
    if not args.quiet and not use_tui:
        print_banner()

    import os

    workspace_arg = args.workspace or os.environ.get("HARNESS_WORKSPACE") or None
    issue_arg = args.issue_file or args.issue_path

    # Interactive `make run` with no issue file: the TUI asks for the task in its
    # own input field (paste + Enter) instead of a raw Ctrl-D stdin prompt.
    if not issue_arg and use_tui and sys.stdin is not None and sys.stdin.isatty():
        code = _launch_tui_collect(args, workspace_arg)
        if code is not None:
            return code
        if mode == "on":
            return EXIT_USAGE
        print(f"[{HARNESS_NAME}] note: TUI unavailable; falling back to the stdin prompt.", file=sys.stderr)

    # Task: explicit file wins, then positional path, then stdin.
    try:
        issue = load_issue(issue_arg, stdin_text=None if issue_arg else _read_stdin())
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("usage: make run < issue.md   |   make run ISSUE=issue.md", file=sys.stderr)
        return EXIT_USAGE

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
        if isinstance(exc, GuidanceError):
            # Guidance, not a failure: print the how-to verbatim, no "error:" prefix.
            print(f"\n{HARNESS_NAME}: {exc}\n", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if use_tui:
        code = _launch_tui(args, options, issue)
        if code is not None:
            return code
        if mode == "on":
            return EXIT_USAGE
        print(f"[{HARNESS_NAME}] note: TUI unavailable; continuing headless.", file=sys.stderr)

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
