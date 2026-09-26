"""
Run orchestration — one function that takes an issue and returns a report.

Why it exists: the entrypoint, the fixture E2E script and the smoke script all
need the same lifecycle (guard the workspace, build the layers, run the agent,
capture the patch, verify, report). Keeping it here means the evaluation path
and our test path cannot drift apart.

Hackathon criteria served: Phase 0.3 (run the agent end-to-end unattended and
write reports/), Phase 4.2 (report artefact), Phase 6.1 (shared by tests).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from minisweagent.agents.default import AgentConfig
from minisweagent.exceptions import LimitsExceeded

from harness import HARNESS_NAME, UPSTREAM_COMMIT, __version__
from harness.agent import HarnessAgent
from harness.budget import Budget, BudgetTracker
from harness.compaction.compactor import Compactor
from harness.config import (
    ConfigError,
    REPO_ROOT,
    build_model_config,
    build_prompts,
    load_compaction_config,
    load_harness_config,
    load_policy_config,
    read_env,
)
from harness.context.repo_map import build_repo_map
from harness.environment import GuardedEnvironment
from harness.guardrails.injection import InjectionScanner
from harness.guardrails.policy import ActionPolicy
from harness.guardrails.scope import ScopeGuard
from harness.issue import Issue, load_issue
from harness.laya.fallback import build_judge
from harness.model import HarnessModel
from harness.report import write_report
from harness.telemetry import Telemetry
from harness.verify import VerificationLoop, Verifier

#: Sentinel commands the guarded environment understands (Phase 3.2).
SENTINELS: tuple[str, ...] = ("harness_load_issue", "harness_submit_patch")

HARNESS_REPO_MARKERS = ("harness/entrypoint.py", "harness/config/harness.yaml")
PATCH_MAX_BYTES = 400_000
UNTRACKED_FILE_MAX_BYTES = 200_000


class UsageError(RuntimeError):
    """Bad invocation (exit code 2). Message is safe to print."""


@dataclass
class RunOptions:
    issue: Issue
    workspace: Path
    reports_root: Path = field(default_factory=lambda: REPO_ROOT / "reports")
    budget_minutes: float | None = None
    max_steps: int | None = None
    verify_mode: str = "auto"
    dry_run: bool = False
    scenario: list[dict] | None = None
    force: bool = False
    quiet: bool = False


@dataclass
class RunResult:
    status: str
    exit_status: str
    run_id: str
    run_dir: Path
    telemetry_path: Path
    report_path: Path | None
    patch_path: Path | None
    submission: str
    stats: dict
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_status": self.exit_status,
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "report": str(self.report_path) if self.report_path else "",
            "telemetry": str(self.telemetry_path),
            "patch": str(self.patch_path) if self.patch_path else "",
            "submission": self.submission[:2000],
            "stats": self.stats,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# workspace guards
# --------------------------------------------------------------------------
def looks_like_harness_repo(path: Path) -> bool:
    return all((path / marker).exists() for marker in HARNESS_REPO_MARKERS)


CLONE_TIMEOUT_S = 300


def _clone_workspace(repo_url: str) -> Path | None:
    """Clone the repository named by the issue into the project-local cache.

    Used only when no workspace was otherwise resolved (no WORKSPACE, no
    ``Workspace:`` hint, no existing checkout in the issue). Failure is not
    fatal: the caller falls back to the current directory with a warning.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", re.sub(r"^https?://github\.com/", "", repo_url).rstrip("/"))
    slug = slug.strip("-") or "target"
    destination = REPO_ROOT / ".cache" / "workspaces" / slug
    if (destination / ".git").exists():
        print(f"[{HARNESS_NAME}] reusing cloned workspace {destination}", file=sys.stderr)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(destination)],
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_S,
        )
    except Exception as exc:
        print(f"[{HARNESS_NAME}] could not clone {repo_url}: {type(exc).__name__}", file=sys.stderr)
        return None
    if proc.returncode != 0 or not (destination / ".git").exists():
        shutil.rmtree(destination, ignore_errors=True)
        print(f"[{HARNESS_NAME}] could not clone {repo_url} (git exit {proc.returncode})", file=sys.stderr)
        return None
    print(f"[{HARNESS_NAME}] cloned issue repository into {destination}", file=sys.stderr)
    return destination


def resolve_workspace(explicit: str | None, issue: Issue, force: bool = False) -> Path:
    """Pick the repo to operate on.

    Resolution order (first hit wins):
      1. explicit ``--workspace`` / ``WORKSPACE`` / ``HARNESS_WORKSPACE``
      2. ``Workspace:``/``Repo: <path>`` hint in the issue text
      3. an existing git checkout whose absolute path is named in the issue
      4. the current directory (the evaluator may run the harness inside the target)
      5. the GitHub repository named by the issue (cloned into ``.cache/workspaces``)
      6. the current directory with a warning — ``make run`` must never refuse to launch.
    """
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_dir():
            raise UsageError(f"workspace is not a directory: {candidate}")
        if looks_like_harness_repo(candidate) and not force:
            raise UsageError(
                f"workspace {candidate} is the guarded-mini repo itself. Refusing to modify our own "
                "checkout. Point it at the target repository instead, e.g.\n"
                f"    make run WORKSPACE=/path/to/target-repo < issue.md\n"
                "or pass --force if you really mean it."
            )
        return candidate
    if issue.workspace_hint:
        candidate = Path(issue.workspace_hint).expanduser().resolve()
        if not candidate.is_dir():
            raise UsageError(f"issue workspace hint is not a directory: {candidate}")
        return candidate
    for raw in issue.absolute_paths:
        try:
            candidate = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if candidate.is_dir() and (candidate / ".git").exists() and not looks_like_harness_repo(candidate):
            print(f"[{HARNESS_NAME}] using git checkout named in the issue: {candidate}", file=sys.stderr)
            return candidate
    cwd = Path.cwd().resolve()
    if not looks_like_harness_repo(cwd) or force:
        return cwd
    if issue.repo_url:
        cloned = _clone_workspace(issue.repo_url)
        if cloned is not None:
            return cloned
    print(
        f"[{HARNESS_NAME}] warning: no target workspace was given and the current directory is the "
        "harness checkout. Running on the current directory; pass WORKSPACE=<target-repo> (or put "
        "'Workspace: /path' in the issue) to point the harness at the repository under test.",
        file=sys.stderr,
    )
    return cwd


# --------------------------------------------------------------------------
# git helpers: changed files + patch artefact
# --------------------------------------------------------------------------
def _git(workspace: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, text=True, timeout=60, check=check
    )


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists() and shutil.which("git") is not None


def collect_changes(workspace: Path, patch_path: Path, exclude_paths: set[str] | None = None) -> dict:
    """Return changed files, diff text and the written patch artefact.

    ``exclude_paths`` removes specific paths from the *patch artefact only* (the
    workspace is left untouched). The scope guard uses this for changes it flagged
    as out of scope but deliberately did not roll back.
    """
    if not is_git_repo(workspace):
        return {"files": [], "diff": "", "path": "", "note": "workspace is not a git checkout; no patch captured"}

    from harness.guardrails.scope import CACHE_PATTERNS

    excluded = {p for p in (exclude_paths or set()) if p}

    def ignored(path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in CACHE_PATTERNS) or path == ".guarded-mini-marker"

    def pathspecs() -> list[str]:
        # `-- .` keeps at least one positive pathspec; excludes are literal paths.
        return ["--", "."] + [f":(exclude){path}" for path in sorted(excluded)]

    status = _git(workspace, "status", "--porcelain=v1", "-uall")
    files: list[dict] = []
    untracked: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2].strip(), line[3:].strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if ignored(path) or path in excluded:
            continue
        if code == "??":
            untracked.append(path)
        files.append({"status": code, "path": path, "additions": "", "deletions": ""})

    numstat = _git(workspace, "diff", "--numstat", *pathspecs())
    numstat_map: dict[str, tuple[str, str]] = {}
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            numstat_map[parts[2]] = (parts[0], parts[1])
    for entry in files:
        if entry["path"] in numstat_map:
            entry["additions"], entry["deletions"] = numstat_map[entry["path"]]

    diff = _git(workspace, "diff", "--no-color", "--no-ext-diff", *pathspecs()).stdout
    new_file_diffs: list[str] = []
    for rel in untracked:
        if rel in excluded:
            continue
        absolute = workspace / rel
        if not absolute.is_file() or absolute.stat().st_size > UNTRACKED_FILE_MAX_BYTES:
            continue
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text:
            continue
        lines = text.splitlines()
        if not lines:
            continue
        new_file_diffs.append(
            f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
        )
        for entry in files:
            if entry["path"] == rel:
                entry["additions"], entry["deletions"] = str(len(lines)), "0"
    if new_file_diffs:
        diff = diff + "\n".join(new_file_diffs)

    truncated = False
    if len(diff) > PATCH_MAX_BYTES:
        diff = diff[:PATCH_MAX_BYTES]
        truncated = True
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff, encoding="utf-8")
    return {
        "files": files,
        "diff": diff,
        "path": str(patch_path),
        "truncated": truncated,
        "note": f"{len(files)} changed file(s)",
    }


# --------------------------------------------------------------------------
# main lifecycle
# --------------------------------------------------------------------------
def _slug(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (text[:24] or "run").strip("-")


def _resolve_budget(config: dict, options: RunOptions, telemetry: Telemetry) -> Budget:
    cfg = dict(config.get("budget") or {})
    if options.budget_minutes is not None:
        cfg["max_wall_seconds"] = max(30, int(options.budget_minutes * 60))
    if options.max_steps is not None:
        cfg["max_steps"] = int(options.max_steps)
    budget = Budget.from_config(cfg)
    telemetry.emit("budget_plan", budget=budget.__dict__, cli_overrides={
        "budget_minutes": options.budget_minutes, "max_steps": options.max_steps,
    })
    return budget


def execute_run(options: RunOptions, telemetry: Telemetry | None = None, on_ready=None) -> RunResult:
    """Run the harness end to end. Never raises for agent-level failures."""
    config = load_harness_config()
    workspace = options.workspace
    # Fail fast on missing credentials *before* any heavy layer (the judge would
    # otherwise load the ~800MB laya checkpoint for nothing). Dry-run needs none.
    env_vars = None if options.dry_run else read_env()
    started = time.time()
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + _slug(options.issue.title or workspace.name)
    run_dir = options.reports_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    telemetry = telemetry or Telemetry(run_dir / config["telemetry"]["jsonl_name"], run_id)
    telemetry.emit(
        "run_start",
        harness={"name": "guarded-mini", "version": __version__},
        upstream={"name": "mini-swe-agent", "commit": UPSTREAM_COMMIT},
        workspace=str(workspace),
        dry_run=options.dry_run,
        verify_mode=options.verify_mode,
        issue=options.issue.to_dict(),
        config_digest={
            "temperature": config["model"]["model_kwargs"].get("temperature"),
            "seed": config["model"]["model_kwargs"].get("seed"),
            "budget": config.get("budget"),
        },
    )
    (options.reports_root / "LATEST").write_text(str(run_dir) + "\n", encoding="utf-8")

    repo_map = build_repo_map(workspace)
    telemetry.emit("repo_map", chars=len(repo_map), preview=repo_map[:500])

    verifier = Verifier(workspace, config.get("verification"), telemetry=telemetry)
    if options.verify_mode == "off":
        verifier.command = ""
    verification_loop = VerificationLoop(verifier, mode=options.verify_mode, telemetry=telemetry)
    telemetry.emit("verification_plan", mode=options.verify_mode, command=verifier.command, test_kind=verifier.kind)

    budget = _resolve_budget(config, options, telemetry)
    budget_tracker = BudgetTracker(budget, telemetry)

    # ---- Phase 1: guardrails ------------------------------------------------
    policy_config = load_policy_config()
    judge = build_judge(policy_config, telemetry=telemetry, task_excerpt=options.issue.text[:300])
    action_policy = ActionPolicy(
        policy_config,
        workspace=workspace,
        judge=judge,
        telemetry=telemetry,
        unattended=bool(policy_config.get("guardrails", {}).get("unattended", True)),
    )
    scanner = InjectionScanner(
        judge=judge,
        telemetry=telemetry,
        threshold=float(policy_config.get("guardrails", {}).get("judge", {}).get("injection_threshold", 0.7)),
    )
    issue_scan = scanner.scan_text(options.issue.text, where="issue")
    issue_warnings = scanner.quarantine_block(issue_scan, source="issue-text") if issue_scan["flagged"] else ""
    scope_guard = ScopeGuard(
        workspace,
        options.issue,
        judge=judge,
        telemetry=telemetry,
        rollback=bool((config.get("scope") or {}).get("rollback", True)),
        ignore_globs=(config.get("scope") or {}).get("ignore_globs", []),
    )
    telemetry.emit(
        "guardrails_armed",
        judge=(judge.status() if judge is not None else None),
        policy_mode=action_policy.mode,
        injection_threshold=scanner.threshold,
        scope_rollback=scope_guard.rollback_enabled,
        issue_flagged=issue_scan["flagged"],
        issue_matches=issue_scan["matches"],
    )

    prompts = build_prompts()
    agent_cfg = {
        "system_template": prompts["system_template"],
        "instance_template": prompts["instance_template"],
        "step_limit": budget.max_steps or 0,
        "wall_time_limit_seconds": budget.max_wall_seconds or 0,
        "max_consecutive_format_errors": int(config["agent"].get("max_consecutive_format_errors", 3)),
        "cost_limit": float(config["agent"].get("cost_limit", 0.0)),
        "output_path": run_dir / config["telemetry"]["trajectory_name"],
    }

    if options.dry_run:
        from harness.dryrun import build_dry_run_model

        model = build_dry_run_model(options.scenario, prompts["observation_template"], telemetry=telemetry)
        model_name = "dry-run (scripted, no network)"
    else:
        model_cfg = build_model_config(config, env_vars)
        model = HarnessModel(
            telemetry=telemetry,
            observation_template=prompts["observation_template"],
            format_error_template=prompts["format_error_template"],
            **model_cfg,
        )
        model_name = model_config_display(model_cfg)

    sentinels = build_sentinels(
        options.issue,
        workspace,
        run_dir,
        verifier=verifier,
        patch_path=run_dir / config["telemetry"]["patch_name"],
        telemetry=telemetry,
    )
    env = GuardedEnvironment(
        workspace=workspace,
        max_output_chars=int(config["environment"].get("max_output_chars", 20000)),
        command_timeout=int(config["environment"].get("command_timeout", 60)),
        telemetry=telemetry,
        policy=action_policy,
        sentinels=sentinels,
        postprocessors=[verification_loop.postprocessor, scanner.observation_postprocessor],
        extra_path=Path(sys.executable).parent,
    )

    # ---- Phase 2: calibrated compaction --------------------------------------
    compaction_config = load_compaction_config()
    compactor = Compactor(
        compaction_config,
        judge=judge,
        telemetry=telemetry,
        task_excerpt=options.issue.text[:300],
        scope_terms=options.issue.scope_terms(),
    )
    telemetry.emit(
        "compaction_plan",
        mode=compactor.mode,
        target_tokens=compactor.target_tokens,
        recent_tokens=compactor.recent_tokens,
        judge=(judge.status() if judge is not None else None),
    )

    agent = HarnessAgent(
        model,
        env,
        config_class=AgentConfig,
        telemetry=telemetry,
        budget_tracker=budget_tracker,
        compactor=compactor,
        **agent_cfg,
    )
    agent.extra_template_vars |= {
        "repo_map": repo_map,
        "issue_meta": options.issue.metadata_markdown(),
        "issue_warnings": issue_warnings,
        "harness_sentinels": ", ".join(SENTINELS),
    }
    if on_ready is not None:
        on_ready(agent, env, run_dir)

    status, exit_status, submission, error = "error", "", "", ""
    try:
        result = agent.run(task=options.issue.text)
        exit_status = str(result.get("exit_status", ""))
        submission = str(result.get("submission", "") or "")
        if exit_status == "Submitted":
            status = "submitted"
        elif exit_status in {"LimitsExceeded", "TimeExceeded"}:
            status = "limits_exceeded"
        elif exit_status:
            status = "format_error"
        else:
            status = "error"
    except LimitsExceeded as exc:  # pragma: no cover - upstream handles this inside run()
        status, exit_status = "limits_exceeded", "LimitsExceeded"
        error = str(exc)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        telemetry.emit("error", error=error)
    finally:
        try:
            env.reap_stragglers()
        except Exception:
            pass

    # final verification evidence (Phase 3 also runs it after edits)
    if verifier.enabled:
        last_result = verifier.run(reason="final")
        if status == "submitted" and not last_result.ok:
            telemetry.emit("verification_failed_after_submit", command=last_result.command)

    patch = collect_changes(workspace, run_dir / config["telemetry"]["patch_name"])
    telemetry.emit("workspace_changes", files=patch.get("files", []), note=patch.get("note", ""))

    scope_summary = scope_guard.enforce(patch.get("files", []))
    if scope_summary.get("rolled_back"):
        # re-collect so the report and patch reflect the final, scoped diff
        patch = collect_changes(workspace, run_dir / config["telemetry"]["patch_name"])
        telemetry.emit("workspace_changes_after_rollback", files=patch.get("files", []))
    # Non-deterministic (judge) out-of-scope flags are deliberately not rolled back
    # — a wrong rollback is worse than a reported oddity — but when a deterministic
    # in-scope change anchors our reading of the task, they must not ship inside the
    # submitted patch either. Exclude them from the patch artefact only.
    excluded_from_patch: list[str] = []
    if scope_summary.get("anchored"):
        excluded_from_patch = sorted(
            entry["path"]
            for entry in scope_summary.get("files", [])
            if not entry.get("in_scope") and entry.get("source") not in ("deterministic",)
        )
    if excluded_from_patch:
        patch = collect_changes(
            workspace, run_dir / config["telemetry"]["patch_name"], exclude_paths=set(excluded_from_patch)
        )
        scope_summary["patch_exclusions"] = excluded_from_patch
        telemetry.emit("patch_exclusions", paths=excluded_from_patch)

    duration = round(time.time() - started, 1)
    # The report must name the provider that actually served the run: the
    # DeepSeek/Qwen discovery fallback can switch mid-run.
    model_name = getattr(model, "active_model_name", None) or model_name
    cost_total = sum(float(e.get("cost") or 0.0) for e in telemetry.events_of("model_call"))
    stats = {
        **budget_tracker.state.snapshot(budget),
        "total_tokens": budget_tracker.state.prompt_tokens + budget_tracker.state.completion_tokens,
        "estimated_usage": budget_tracker.state.estimated_usage,
        "cost_total": round(cost_total, 6),
        "verification_runs": verifier.runs,
        "patch_bytes": len(patch.get("diff", "")),
        "duration_s": duration,
    }
    telemetry.emit("run_end", status=status, exit_status=exit_status, stats=stats, error=error)

    ctx = {
        "run_id": run_id,
        "status": status,
        "exit_status": exit_status,
        "workspace": str(workspace),
        "model_name": model_name,
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_s": duration,
        "issue": options.issue.to_dict() | {"text": options.issue.text},
        "stats": stats,
        "patch": patch,
        "verification": {
            "command": verifier.command,
            "kind": verifier.kind,
            "history": [
                {"reason": e.get("reason"), "ok": e.get("ok"), "returncode": e.get("returncode"), "duration_s": e.get("duration_s")}
                for e in telemetry.events_of("verification")
            ],
            "last": verifier.last.to_dict() if verifier.last else {},
        },
        "guardrails": telemetry.events_of("command"),
        "guardrail_decisions": telemetry.events_of("guardrail"),
        "reproducibility": {
            "model": model_name,
            "temperature": config["model"]["model_kwargs"].get("temperature"),
            "seed": config["model"]["model_kwargs"].get("seed"),
            "upstream": f"mini-swe-agent {UPSTREAM_COMMIT[:12]}",
            "calibration": (judge.calibration.get("provenance", "unknown") if judge is not None and hasattr(judge, "calibration") else "n/a"),
        },
        "injection": {
            "issue_flagged": issue_scan["flagged"],
            "issue_matches": issue_scan["matches"],
            "scans": telemetry.events_of("injection_scan"),
            "threshold": scanner.threshold,
        },
        "scope": scope_summary,
        "judge": judge.status() if judge is not None else {"available": False, "mode": "disabled"},
        "compaction": {**compactor.status(), "last": (telemetry.events_of("compaction") or [{}])[-1]},
        "degradations": telemetry.events_of("degradation"),
        "budget": budget.__dict__,
        "timeline": telemetry.events[-60:],
        "submission": submission,
        "telemetry_path": str(telemetry.path),
        "upstream_commit": UPSTREAM_COMMIT,
        "dry_run": options.dry_run,
    }
    report_path = write_report(run_dir, config["telemetry"]["report_name"], ctx)
    telemetry.emit("report_written", path=str(report_path))

    result = RunResult(
        status=status,
        exit_status=exit_status,
        run_id=run_id,
        run_dir=run_dir,
        telemetry_path=telemetry.path,
        report_path=report_path,
        patch_path=Path(patch["path"]) if patch.get("path") else None,
        submission=submission,
        stats=stats,
        error=error,
    )
    (run_dir / "status.json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return result


def model_config_display(model_cfg: dict) -> str:
    """Model name without any credential material (there is none in the name)."""
    return str(model_cfg.get("model_name", ""))


def build_sentinels(issue: Issue, workspace: Path, run_dir: Path, *, verifier=None, patch_path: Path | None = None, telemetry=None) -> dict:
    """Sentinel commands the agent can run besides bash (Phase 3.2)."""

    def load_issue_sentinel(_argument: str) -> dict:
        return {
            "output": f"<issue source=\"{issue.source}\">\n{issue.text}\n</issue>\n\n"
            + "Parsed metadata:\n"
            + issue.metadata_markdown(),
            "returncode": 0,
        }

    def submit_patch_sentinel(_argument: str) -> dict:
        patch = collect_changes(workspace, patch_path or (run_dir / "patch.diff"))
        lines = [
            f"Final patch: {len(patch.get('diff', ''))} bytes across {len(patch.get('files') or [])} file(s).",
        ]
        if patch.get("files"):
            lines.append("Files: " + ", ".join(f"{f['status']} {f['path']}" for f in patch["files"][:20]))
        if verifier is not None and verifier.enabled:
            result = verifier.run(reason="submit_patch")
            lines.append(f"Final verification: {'PASS' if result.ok else 'FAIL'} ({result.command})")
            lines.append(result.summary(1500))
        elif verifier is not None:
            lines.append("No test command could be detected; no automated verification evidence.")
        if telemetry is not None:
            telemetry.emit("submit_patch", files=patch.get("files", []), bytes=len(patch.get("diff", "")))
        return {"output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n" + "\n".join(lines), "returncode": 0}

    return {"harness_load_issue": load_issue_sentinel, "harness_submit_patch": submit_patch_sentinel}
