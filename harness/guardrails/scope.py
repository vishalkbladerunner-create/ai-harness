"""
Scope guardrail — only files plausibly related to the issue may change (Phase 1.2).

Why it exists: a guarded harness must not let the model "fix" unrelated files.
The end-of-run diff check classifies every change deterministically first (paths
named in the issue, their tests, their direct directories) and asks laya only
about the grey zone. Deterministic out-of-scope changes are rolled back in git
workspaces; judge-only flags are reported but not reverted (a wrong rollback is
worse than a reported oddity — the conservative direction).

Hackathon criteria served: Phase 1.2 (scope guardrail + rollback), Phase 4.2
(flagged/rolled-back evidence in the report).
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from harness.guardrails.judgments import scope_question

CACHE_PATTERNS = (
    ".git/**",
    "__pycache__/**",
    "*.pyc",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    "node_modules/**",
    ".venv/**",
    "venv/**",
    "htmlcov/**",
    ".coverage",
)


class ScopeGuard:
    def __init__(self, workspace: Path, issue, *, judge=None, telemetry=None, rollback: bool = True, ignore_globs: list[str] | None = None):
        self.workspace = Path(workspace)
        self.issue = issue
        self.judge = judge
        self.telemetry = telemetry
        self.rollback_enabled = rollback
        self.ignore_globs = tuple(CACHE_PATTERNS) + tuple(ignore_globs or [])
        self.terms = [t.lower() for t in (issue.scope_terms() if issue else [])]
        self.issue_text = (issue.text if issue else "").lower()

    # ------------------------------------------------------------------ API
    def enforce(self, changed_files: list[dict]) -> dict:
        """Classify, report, and (deterministically) roll back out-of-scope changes."""
        relevant = [f for f in changed_files if not self._ignored(f["path"])]
        ignored = [f["path"] for f in changed_files if self._ignored(f["path"])]
        classified = [self._classify(entry) for entry in relevant]
        # Conservative rollback rule: a judge-less out-of-scope verdict is only
        # actionable when at least one change is deterministically in scope, i.e.
        # we have an anchor proving we understood the issue's scope. Otherwise the
        # verdict is report-only (a wrong rollback is worse than a reported oddity).
        anchored = any(entry["in_scope"] and entry["source"] == "deterministic" for entry in classified)
        for entry in classified:
            if anchored and entry["source"] == "heuristic-flag" and not entry["in_scope"]:
                entry["source"] = "deterministic"
                entry["reason"] = "not named by the issue, relative to deterministic in-scope changes"
        flagged = [entry for entry in classified if not entry["in_scope"]]
        rolled_back: list[dict] = []
        if self.rollback_enabled:
            for entry in flagged:
                if entry["source"] == "deterministic" and entry["status"] in ("M", "D", "MM", "MD"):
                    if self._git_checkout(entry["path"]):
                        rolled_back.append({"path": entry["path"], "action": "restored from git"})
                elif entry["source"] == "deterministic" and entry["status"] == "??":
                    if self._delete_untracked(entry["path"]):
                        rolled_back.append({"path": entry["path"], "action": "deleted (untracked, out of scope)"})
        summary = {
            "files": classified,
            "flagged": [e["path"] for e in flagged],
            "rolled_back": rolled_back,
            "ignored": ignored,
            "in_scope": len(classified) - len(flagged),
            "anchored": anchored,
            "mode": "git-rollback" if self.rollback_enabled else "report-only",
        }
        if self.telemetry is not None:
            self.telemetry.emit(
                "scope_check",
                in_scope=summary["in_scope"],
                flagged=summary["flagged"],
                rolled_back=rolled_back,
                ignored=ignored,
                files=[{"path": e["path"], "in_scope": e["in_scope"], "source": e["source"], "reason": e["reason"]} for e in classified],
            )
        return summary

    # ------------------------------------------------------------- internals
    def _ignored(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.ignore_globs)

    def _classify(self, entry: dict) -> dict:
        path = entry["path"]
        lower = path.lower()
        in_terms = any(term and (term in lower or Path(path).name.lower() == term or Path(path).stem.lower() == term) for term in self.terms)
        if in_terms:
            return {**entry, "in_scope": True, "source": "deterministic", "reason": "path is named by the issue", "probability": 0.0}
        if self._test_of_changed(path):
            return {**entry, "in_scope": True, "source": "deterministic", "reason": "test file covering an in-scope change", "probability": 0.0}
        if self._config_adjacent(path):
            return {**entry, "in_scope": True, "source": "deterministic", "reason": "project config the change depends on", "probability": 0.1}

        judgement = self._second_opinion(entry)
        if judgement and judgement.get("source") != "unavailable":
            p_in = float(judgement.get("p_true", 0.5))
            return {
                **entry,
                "in_scope": p_in >= 0.5,
                "source": judgement.get("source", "judge"),
                "reason": f"judge P(in_scope)={p_in:.3f}",
                "probability": p_in,
            }
        # No judge: conservative — flag for the report, but tag it as judge-less so no rollback.
        return {**entry, "in_scope": False, "source": "heuristic-flag", "reason": "not named by the issue and no judge available", "probability": None}

    def _test_of_changed(self, path: str) -> bool:
        name = Path(path).name.lower()
        if not (name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path.lower()}"):
            return False
        mentioned_tests = [t.lower() for t in (self.issue.tests if self.issue else [])]
        if any(t in name for t in mentioned_tests):
            return True
        return any(term and term in self.issue_text for term in ("test", "pytest", "unittest"))

    def _config_adjacent(self, path: str) -> bool:
        name = Path(path).name.lower()
        return name in {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "requirements.txt", "package.json"}

    def _second_opinion(self, entry: dict) -> dict | None:
        if self.judge is None or not getattr(self.judge, "available", False):
            return None
        try:
            state = (
                f"Task: {self.issue.text[:400] if self.issue else ''}\n"
                f"Changed file: {entry['path']} (status {entry.get('status', '?')}, "
                f"+{entry.get('additions', '?')}/-{entry.get('deletions', '?')})"
            )
            state = state[: getattr(self.judge, "max_state_chars", 1100)]
            result = self.judge.judge_batch([state], scope_question())[0]
        except Exception as exc:
            if self.telemetry is not None:
                self.telemetry.degrade("scope_judge", f"{type(exc).__name__}: {exc}")
            return None
        if not result or result.get("source") == "unavailable":
            return None
        answer = (result.get("answers") or {}).get("in_scope") or {}
        return {"p_true": answer.get("p_true", 0.5), "label": answer.get("label"), "source": result.get("source")}

    # ------------------------------------------------------------- rollback
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.workspace), *args], capture_output=True, text=True, timeout=30)

    def _git_checkout(self, path: str) -> bool:
        result = self._git("checkout", "--", path)
        if result.returncode != 0:
            if self.telemetry is not None:
                self.telemetry.degrade("scope_rollback", f"git checkout failed for {path}: {result.stderr.strip()[:200]}")
            return False
        return True

    def _delete_untracked(self, path: str) -> bool:
        absolute = (self.workspace / path).resolve()
        if not str(absolute).startswith(str(self.workspace.resolve())):
            return False
        try:
            if absolute.is_file() or absolute.is_symlink():
                absolute.unlink()
                return True
        except OSError:
            return False
        return False
