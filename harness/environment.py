"""
Guarded environment — the single choke point for everything the agent executes.

Why it exists: in mini-swe-agent, tool calls are bash commands executed by an
``Environment``. Wrapping that one class gives us a mandatory interception point
for the entire guardrail stack (policy check, secret hygiene, telemetry,
self-verification, sentinels) without modifying the vendored core. The
"what happens between the model returning a tool call and the tool running?"
question has exactly one answer: ``GuardedEnvironment.execute``.

Hackathon criteria served: Phase 1.1 (action gating), 1.4 (reaping, timeouts),
1.5 (secret hygiene), Phase 3.2 (harness sentinels), Phase 4.1 (telemetry).

Phase 0 wires the mechanism (secret stripping, telemetry, sentinels, output
post-processing); the policy object that decides allow/ask/refuse is attached in
Phase 1 and defaults to ``None`` (allow-all, logged as such).
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig

from harness.secrets import SENSITIVE_NAME_FRAGMENTS, mask_text

HARNESS_SENTINEL_PREFIX = "harness_"


class GuardedEnvironmentConfig(LocalEnvironmentConfig):
    workspace: str = ""
    max_output_chars: int = 20000


def _is_sensitive_name(name: str) -> bool:
    upper = name.upper()
    return any(fragment in upper for fragment in SENSITIVE_NAME_FRAGMENTS)


def _parse_elapsed(text: str) -> int | None:
    """Parse BSD ``ps -o etime`` ([[dd-]hh:]mm:ss) into seconds."""
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        hours, minutes, seconds = nums
    elif len(nums) == 2:
        hours, (minutes, seconds) = 0, nums
    elif len(nums) == 1:
        hours, minutes, seconds = 0, 0, nums[0]
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class GuardedEnvironment(LocalEnvironment):
    """Execution wrapper: policy gate -> subprocess -> hygiene -> telemetry."""

    def __init__(
        self,
        *,
        workspace: str | Path = ".",
        max_output_chars: int = 20000,
        telemetry=None,
        policy=None,
        sentinels: dict[str, Callable[[str], dict]] | None = None,
        postprocessors: list[Callable[[str, dict, dict], list[str]]] | None = None,
        config_class: type = GuardedEnvironmentConfig,
        **kwargs,
    ):
        self.workspace = str(Path(workspace).resolve())
        self.telemetry = telemetry
        self.policy = policy
        self.sentinels = sentinels or {}
        self.postprocessors = postprocessors or []
        self.max_output_chars = max_output_chars
        self.command_count = 0
        self._created_at = time.time()
        self._stragglers_killed: list[dict] = []

        # Credential hygiene, upstream-compatible: LocalEnvironment executes with
        # `os.environ | config.env`, so *omitting* a variable is not enough —
        # config.env must explicitly blank it. We therefore blank every
        # credential-like name present in the current process environment.
        child_env = dict(kwargs.pop("env", {}) or {})
        for name in os.environ:
            if _is_sensitive_name(name):
                child_env[name] = ""
        extra_path = kwargs.pop("extra_path", None)
        if extra_path:
            # Put the harness venv first so `python`, `python3` and `pytest` mean the
            # same interpreter our verifier uses (documented in the README).
            child_env["PATH"] = str(extra_path) + os.pathsep + os.environ.get("PATH", "")
            child_env.setdefault("VIRTUAL_ENV", str(Path(extra_path).parent))
        kwargs["env"] = child_env
        kwargs.setdefault("cwd", self.workspace)
        kwargs.setdefault("timeout", 60)
        super().__init__(config_class=config_class, workspace=self.workspace,
                         max_output_chars=max_output_chars, **kwargs)

    # ------------------------------------------------------------------ API
    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = str(action.get("command", "") or "")
        self.command_count += 1
        started = time.time()

        # 1) harness sentinels short-circuit bash entirely
        stripped = command.strip()
        if stripped.startswith(HARNESS_SENTINEL_PREFIX):
            output = self._run_sentinel(stripped)
            final = self._finalise(command, output)
            self._emit_command_event(command, final, started, decision="sentinel")
            # sentinels can submit (upstream's documented check: first line marker + rc 0)
            self._check_finished(final)
            return final

        # 2) policy gate (Phase 1). None -> allow-all, still recorded.
        decision = {"outcome": "allow", "reason": "policy disabled (no policy object)", "source": "none"}
        if self.policy is not None:
            decision = self.policy.check(command)
            if decision.get("outcome") == "refuse":
                output = self._refusal_output(command, decision)
                self._emit_command_event(command, output, started, decision="refuse", policy=decision)
                return self._finalise(command, output)

        # 3) real execution via upstream LocalEnvironment (process-group kill on timeout)
        output = super().execute(action, cwd=cwd, timeout=timeout)
        self._emit_command_event(command, output, started, decision=decision.get("outcome", "allow"), policy=decision)
        return self._finalise(command, output)

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        vars_ = super().get_template_vars(**kwargs)
        # Never expose credential-like values to prompt templates either.
        return {k: v for k, v in vars_.items() if not _is_sensitive_name(k)}

    # ------------------------------------------------------------- internals
    def _run_sentinel(self, command: str) -> dict:
        name, _, argument = command.partition(" ")
        handler = self.sentinels.get(name)
        if handler is None:
            known = ", ".join(sorted(self.sentinels)) or "(none)"
            return {
                "output": f"Unknown harness command '{name}'. Available harness commands: {known}",
                "returncode": 1,
                "exception_info": "",
            }
        try:
            result = handler(argument.strip())
        except Exception as exc:  # a sentinel failure must not kill the run
            return {"output": f"harness command '{name}' failed: {exc}", "returncode": 1, "exception_info": ""}
        result.setdefault("returncode", 0)
        result.setdefault("exception_info", "")
        return result

    def _finalise(self, command: str, output: dict) -> dict:
        text = str(output.get("output") or "")
        text = mask_text(text)
        extras: list[str] = []
        for post in self.postprocessors:
            try:
                extras.extend(post(command, output, {"workspace": self.workspace}) or [])
            except Exception as exc:  # postprocessing must never break a run
                extras.append(f"<postprocess-error>{type(exc).__name__}: {exc}</postprocess-error>")
        if extras:
            text = text + "\n" + "\n".join(extras)
        text = self._elide(text)
        output["output"] = text
        if isinstance(output.get("extra"), dict):
            output["extra"]["harness_masked"] = True
        return output

    def _elide(self, text: str) -> str:
        limit = self.max_output_chars
        if limit and len(text) > limit:
            head = text[: limit // 2]
            tail = text[-limit // 2 :]
            return f"{head}\n\n<elided {len(text) - limit} characters>\n\n{tail}"
        return text

    def _refusal_output(self, command: str, decision: dict) -> dict:
        reason = decision.get("reason", "blocked by policy")
        suggestion = decision.get("suggestion", "")
        lines = [
            "POLICY REFUSAL — the command was not executed.",
            f"Command: {command}",
            f"Reason: {reason}",
        ]
        if decision.get("probability") is not None:
            lines.append(f"Calibrated risk probability: {decision['probability']:.3f}")
        if suggestion:
            lines.append(f"Suggested safe alternative: {suggestion}")
        lines.append("Adjust your approach and continue with the task.")
        return {"output": "\n".join(lines), "returncode": 126, "exception_info": ""}

    def _emit_command_event(self, command: str, output: dict, started: float, decision: str, policy: dict | None = None) -> None:
        if self.telemetry is None:
            return
        self.telemetry.emit(
            "command",
            n=self.command_count,
            command=command[:2000],
            decision=decision,
            policy=policy,
            returncode=output.get("returncode"),
            duration_s=round(time.time() - started, 3),
            output_chars=len(str(output.get("output") or "")),
        )

    # ------------------------------------------------------- runaway reaping
    def reap_stragglers(self) -> list[dict]:
        """Kill background processes that survived a command and reference the workspace.

        Conservative by construction: only processes younger than this environment
        whose command line mentions the workspace path are considered, and the
        harness never kills its own process group.
        """
        age_limit = int(time.time() - self._created_at) + 10
        killed: list[dict] = []
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,pgid=,etime=,command="],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return killed
        if proc.returncode != 0:
            return killed
        own_pid, own_pgid = os.getpid(), os.getpgid(0)
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            pid_s, pgid_s, etime_s, cmd = parts
            try:
                pid, pgid = int(pid_s), int(pgid_s)
            except ValueError:
                continue
            if pid in (own_pid,) or pgid in (own_pgid,):
                continue
            seconds = _parse_elapsed(etime_s)
            if seconds is None or seconds > age_limit:
                continue
            if self.workspace not in cmd:
                continue
            try:
                os.killpg(pgid, signal.SIGKILL)
                killed.append({"pid": pid, "pgid": pgid, "command": cmd[:200]})
            except ProcessLookupError:
                continue
            except Exception:
                continue
        self._stragglers_killed.extend(killed)
        if killed and self.telemetry is not None:
            self.telemetry.emit("runaway_reaped", processes=killed)
        return killed
