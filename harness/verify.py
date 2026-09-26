"""
Self-verification — detect the project's tests, run them, feed failures back.

Why it exists: Phase 3.1 requires the harness to run tests automatically after
file edits and feed failures back until green or budget exhaustion. The runner
lives here; wiring happens through the guarded environment's postprocessors so
that verification evidence ends up inside the model's observation.

Because the harness Python venv already contains pytest (and the harness test
dependencies), verification is executed with *that* interpreter by default. The
choice is recorded in telemetry so neither the model nor the report can mistake
which interpreter ran the tests.

Hackathon criteria served: Phase 3.1 (self-verification loop), Phase 4.2 (test
evidence in the report).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MARKERS = {
    "python": ["pytest.ini", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini"],
    "node": ["package.json"],
    "generic": ["Makefile"],
}


@dataclass
class VerificationResult:
    command: str
    returncode: int
    output: str
    duration_s: float
    ok: bool
    ran_at: float = field(default_factory=time.time)

    def summary(self, max_output: int = 3000) -> str:
        text = self.output.strip()
        if len(text) > max_output:
            text = text[: max_output // 2] + "\n...\n" + text[-max_output // 2 :]
        return text

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 2),
            "output_tail": self.summary(1200),
        }


def detect_test_command(workspace: str | Path, config: dict | None = None) -> tuple[str, str]:
    """Return (command, kind). Empty command means "nothing detected"."""
    workspace = Path(workspace)
    config = config or {}
    py_cmd = (config.get("python") or ["python -m pytest -q"])[0]
    node_cmd = (config.get("node") or ["npm test --silent"])[0]
    generic_cmd = (config.get("generic") or ["make test"])[0]

    has_python_tests = any(workspace.glob("tests/test_*.py")) or any(workspace.glob("test_*.py"))
    if has_python_tests:
        return py_cmd, "python"
    if any((workspace / marker).exists() for marker in DEFAULT_MARKERS["python"]):
        return py_cmd, "python"
    if (workspace / "package.json").exists() and shutil.which("npm"):
        return node_cmd, "node"
    if (workspace / "Makefile").exists():
        target = _makefile_has_test_target(workspace / "Makefile")
        if target:
            return generic_cmd, "make"
    return "", ""


def _makefile_has_test_target(path: Path) -> bool:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("test:") or line.startswith("test :"):
                return True
    except OSError:
        return False
    return False


class Verifier:
    """Runs the detected test command with timeout and output bounds."""
    def __init__(self, workspace: str | Path, config: dict | None = None, telemetry=None, interpreter: str | None = None):
        self.workspace = Path(workspace)
        self.config = config or {}
        self.telemetry = telemetry
        self.interpreter = interpreter or sys.executable
        self.command, self.kind = detect_test_command(self.workspace, self.config)
        self.timeout = int(self.config.get("timeout_seconds", 180))
        self.last: VerificationResult | None = None
        self.runs = 0

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def run(self, reason: str) -> VerificationResult:
        command = self.command
        # Prefer the harness interpreter for python projects: it has pytest and
        # the harness dependencies, and keeps the target repo's venv untouched.
        if self.kind == "python":
            command = command.replace("python ", f"{self.interpreter} ", 1)
        started = time.time()
        try:
            # gh pr-* style commands never appear here; this is a local test run.
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = f"verification timed out after {self.timeout}s\n" + str(exc)
            returncode = -1
        duration = time.time() - started
        result = VerificationResult(
            command=command, returncode=returncode, output=output, duration_s=duration, ok=returncode == 0
        )
        self.last = result
        self.runs += 1
        if self.telemetry is not None:
            self.telemetry.emit(
                "verification",
                run=self.runs,
                reason=reason,
                command=command,
                test_kind=self.kind,
                returncode=returncode,
                ok=result.ok,
                duration_s=round(duration, 2),
                output_tail=result.summary(1200),
            )
        return result

    def observation_block(self, result: VerificationResult) -> str:
        status = "PASS" if result.ok else "FAIL"
        return (
            f"<verification status=\"{status}\" command={json.dumps(result.command)} "
            f"returncode={result.returncode}>\n"
            f"{result.summary()}\n"
            f"</verification>"
        )


class VerificationLoop:
    """Phase 3.1: run the project's tests after file changes, feed failures back.

    Hooks into the guarded environment as a postprocessor: whenever a command changed
    the workspace (auto) — or after every command (on) — the test command runs and its
    result is appended to the observation as a `<verification>` block. The agent then
    sees the failure on its next step, which is the feedback loop.

    A command that already *is* the test command is not re-run: its own output is
    recorded as evidence instead (no duplicate executions).
    """

    def __init__(self, verifier: Verifier, *, mode: str = "auto", telemetry=None):
        self.verifier = verifier
        self.mode = mode
        self.telemetry = telemetry
        self.last_fingerprint = self._fingerprint()
        self.auto_runs = 0
        self.skipped_as_duplicate = 0

    # ------------------------------------------------------------------ API
    def postprocessor(self, command: str, output: dict, context: dict) -> list[str]:
        if self.mode == "off" or not self.verifier.enabled:
            return []
        if self._is_test_command(command):
            self._record_model_run(command, output)
            return []
        if self.mode == "auto":
            fingerprint = self._fingerprint()
            changed = fingerprint != self.last_fingerprint
            self.last_fingerprint = fingerprint
            if not changed:
                return []
        result = self.verifier.run(reason=f"after command: {command[:60]}")
        self.auto_runs += 1
        self.last_fingerprint = self._fingerprint()
        return [self.verifier.observation_block(result)]

    # ------------------------------------------------------------- internals
    def _is_test_command(self, command: str) -> bool:
        if not self.verifier.command:
            return False
        tokens = [t for t in re.split(r"[\s;&|]+", self.verifier.command) if t and not t.startswith("-")]
        markers = {"pytest", "make", "npm", "pnpm", "yarn", "unittest"}
        return any(marker in command for marker in markers) and any(t in command for t in tokens[:2])

    def _record_model_run(self, command: str, output: dict) -> None:
        self.skipped_as_duplicate += 1
        if self.telemetry is not None:
            self.telemetry.emit(
                "verification_model_run",
                command=command[:200],
                returncode=output.get("returncode"),
                ok=output.get("returncode") == 0,
            )

    def _fingerprint(self) -> str:
        """Cheap 'did the workspace change?' signal: git status + diff content hash,
        else a find-newer marker. Content-sensitive on purpose: re-editing an already
        modified file must trigger verification again."""
        import hashlib

        workspace = self.verifier.workspace
        try:
            if (workspace / ".git").exists() and shutil.which("git"):
                status = subprocess.run(
                    ["git", "-C", str(workspace), "status", "--porcelain=v1", "-uall"],
                    capture_output=True, text=True, timeout=15,
                ).stdout
                diff = subprocess.run(
                    ["git", "-C", str(workspace), "diff", "--no-color"],
                    capture_output=True, text=True, timeout=30,
                ).stdout
                return hashlib.sha1((status + "\n" + diff).encode("utf-8", "replace")).hexdigest()
        except Exception:
            pass
        marker = workspace / ".guarded-mini-marker"
        try:
            if not marker.exists():
                marker.write_text("marker\n", encoding="utf-8")
            proc = subprocess.run(
                ["find", ".", "-newer", str(marker), "-type", "f", "-not", "-path", "./.git/*", "-not", "-name", "*.pyc"],
                cwd=str(workspace), capture_output=True, text=True, timeout=15,
            )
            return proc.stdout
        except Exception:
            return ""
