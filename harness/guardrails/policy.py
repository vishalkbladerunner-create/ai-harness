"""
Action policy — deterministic rules first, laya second opinion for the grey zone.

Why it exists: Phase 1.1 requires every shell command to pass a policy check
before execution, with deterministic blocklists authoritative and a calibrated
local judge for the commands that fall between "obviously safe" and "obviously
destructive". The decision vocabulary is allow / ask / refuse, and every outcome
carries its probability and reason so the report can prove what happened.

Attended vs unattended: with no human in the loop (the evaluator's case), `ask`
resolves to refuse-with-guidance — the deny path is graceful: the agent receives
an explanation and a suggested safe alternative, and simply continues.

Hackathon criteria served: Phase 1.1 (action gating), Phase 1.5 (no secret
echoing, no git staging), Phase 4.1 (decisions logged with probabilities).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Pipeline separators used to split a compound command into segments.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")

#: Write-ish verbs that make an absolute path outside the workspace dangerous.
_WRITE_VERBS = re.compile(
    r"\b(rm|mv|cp|sed\s+-i|truncate|chmod|chown|mkdir|touch|ln|tee|dd|patch)\b|>>?\s*\S"
)

#: Dynamic evaluation: cannot be statically confined, always judge.
_DYNAMIC = re.compile(r"\b(eval|exec|source)\b|python[0-9.]*\s+-c\b|base64\s+(-d|--decode)|\bxargs\b", re.IGNORECASE)

#: Parent-directory traversal in a write-ish command.
_PARENT_TRAVERSAL = re.compile(r"(^|\s)\.\.(/|\s|$)")

_REDIRECT = re.compile(r">{1,2}\s*([^\s;|&]+)")
_WRITE_CMDS = ("rm", "mv", "cp", "ln", "touch", "mkdir", "chmod", "chown", "tee", "truncate")
_SED_IN_PLACE = re.compile(r"\bsed\s+-i\S*\s+(.*)$")


def extract_write_targets(segment: str) -> list[str]:
    """Candidate filesystem targets of write-ish commands (quotes/sed exprs excluded)."""
    targets: list[str] = []
    for match in _REDIRECT.finditer(segment):
        targets.append(match.group(1).strip("'\""))
    sed = _SED_IN_PLACE.search(segment)
    if sed:
        tokens = [t for t in sed.group(1).split() if not t.startswith("-")]
        if tokens:
            targets.append(tokens[-1].strip("'\""))
    for cmd in _WRITE_CMDS:
        match = re.search(rf"\b{cmd}\b(.*)$", segment)
        if not match:
            continue
        for token in match.group(1).split():
            if token.startswith("-") or token.startswith("$") or "=" in token:
                continue
            if token in ("|", "&&", "||", ";"):
                continue
            targets.append(token.strip("'\""))
    return [t for t in targets if t]


def _target_outside_workspace(target: str, workspace_resolved: str, workspace_alt: str) -> bool:
    if target.startswith(("/dev/null", "/dev/stdout", "/dev/stderr")):
        return False
    if target.startswith(workspace_resolved) or target.startswith(workspace_alt):
        return False
    if not target.startswith("/"):
        return False  # relative to the workspace cwd
    return True


def writes_confined_to_workspace(command: str, workspace: Path | None) -> tuple[bool, str]:
    """True when every write target is inside the workspace (or relative to it).

    Static containment is what makes an edit command *safe by construction*: the
    workspace is a disposable checkout, and the scope guard separately checks that
    only task-related files changed. Dynamic evaluation and parent traversal are
    never confined.
    """
    if workspace is None:
        return False, "no workspace given"
    workspace_resolved = str(Path(workspace).resolve())
    workspace_alt = (
        workspace_resolved.replace("/private/var", "/var", 1)
        if workspace_resolved.startswith("/private/var")
        else str(Path("/private") / workspace_resolved.lstrip("/"))
    )
    for segment in split_segments(command):
        if _DYNAMIC.search(segment):
            return False, "dynamic evaluation cannot be statically confined"
        if _PARENT_TRAVERSAL.search(segment):
            return False, "parent-directory traversal"
        for target in extract_write_targets(segment):
            if _target_outside_workspace(target, workspace_resolved, workspace_alt):
                return False, f"target outside workspace: {target}"
    return True, ""


@dataclass
class Rule:
    id: str
    outcome: str
    reason: str
    suggestion: str
    patterns: list[re.Pattern] = field(default_factory=list)


def split_segments(command: str) -> list[str]:
    """Split a compound command into separately classifiable segments."""
    return [segment.strip() for segment in _SEGMENT_SPLIT.split(command or "") if segment.strip()]


def classify_deterministic(
    command: str,
    rules: list[Rule],
    safe_prefixes: list[re.Pattern],
    risk_markers: list[re.Pattern],
    workspace: Path | None = None,
) -> dict:
    """Return a decision dict using *only* deterministic knowledge.

    outcome is one of: refuse, safe (clear allow), grey (needs the judge).
    """
    segments = split_segments(command) or [command]
    for segment in segments:
        for rule in rules:
            for pattern in rule.patterns:
                if pattern.search(segment):
                    return {
                        "outcome": "refuse",
                        "rule": rule.id,
                        "reason": rule.reason,
                        "suggestion": rule.suggestion,
                        "source": "deterministic",
                        "segment": segment[:200],
                    }
    first = segments[0]
    has_write_marker = any(marker.search(segment) for marker in risk_markers for segment in segments)
    # Edits/writes statically confined to the workspace are the agent's normal work.
    if has_write_marker and workspace is not None:
        confined, why = writes_confined_to_workspace(command, workspace)
        if confined:
            return {
                "outcome": "safe",
                "rule": "in_workspace_write",
                "reason": "all write targets are inside the workspace; scope is checked on the final diff",
                "source": "deterministic",
            }
    if any(pattern.search(first) for pattern in safe_prefixes) and not has_write_marker:
        return {"outcome": "safe", "rule": "safe_prefix", "reason": "read-only inspection command", "source": "deterministic"}
    if has_write_marker:
        return {"outcome": "grey", "rule": "risk_marker", "reason": "command writes or evaluates; needs a second opinion", "source": "deterministic"}
    return {"outcome": "grey", "rule": "unknown", "reason": "command is not on the safe list", "source": "deterministic"}


def outside_workspace_paths(command: str, workspace: Path) -> list[str]:
    """Write targets that live outside the workspace."""
    workspace_resolved = str(workspace.resolve())
    workspace_alt = (
        workspace_resolved.replace("/private/var", "/var", 1)
        if workspace_resolved.startswith("/private/var")
        else str(Path("/private") / workspace_resolved.lstrip("/"))
    )
    found: list[str] = []
    for segment in split_segments(command):
        if not _WRITE_VERBS.search(segment):
            continue
        for target in extract_write_targets(segment):
            if _target_outside_workspace(target, workspace_resolved, workspace_alt):
                found.append(target)
    return found


class ActionPolicy:
    """Gate for `GuardedEnvironment.execute`: allow / ask / refuse, always logged."""

    def __init__(self, config: dict, *, workspace: Path, judge=None, telemetry=None, unattended: bool | None = None):
        cfg = config.get("guardrails", config) or {}
        self.mode = cfg.get("mode", "enforce")
        self.unattended = cfg.get("unattended", True) if unattended is None else unattended
        judge_cfg = cfg.get("judge", {}) or {}
        self.judge_enabled = bool(judge_cfg.get("enabled", True))
        self.refuse_threshold = float(judge_cfg.get("refuse_threshold", 0.85))
        self.ask_threshold = float(judge_cfg.get("ask_threshold", 0.55))
        self.max_command_chars = int(judge_cfg.get("max_command_chars", 400))
        self.judge = judge
        self.telemetry = telemetry
        self.workspace = Path(workspace).resolve()
        self.rules = [
            Rule(
                id=rule["id"],
                outcome=rule.get("outcome", "refuse"),
                reason=rule.get("reason", ""),
                suggestion=rule.get("suggestion", ""),
                patterns=[re.compile(p, re.IGNORECASE) for p in rule.get("patterns", [])],
            )
            for rule in cfg.get("rules", [])
        ]
        self.safe_prefixes = [re.compile(p, re.IGNORECASE) for p in cfg.get("safe_prefixes", [])]
        self.risk_markers = [re.compile(p, re.IGNORECASE) for p in cfg.get("risk_markers", [])]

    # ------------------------------------------------------------------ gate
    def check(self, command: str) -> dict:
        command = command or ""
        decision = classify_deterministic(command, self.rules, self.safe_prefixes, self.risk_markers, self.workspace)
        decision.setdefault("segment", command[:300])

        # workspace containment is enforced deterministically for write-ish commands
        if decision["outcome"] != "refuse":
            stray = outside_workspace_paths(command, self.workspace)
            if stray:
                decision = {
                    "outcome": "refuse",
                    "rule": "outside_workspace_write",
                    "reason": "command writes to path(s) outside the workspace: " + ", ".join(stray[:3]),
                    "suggestion": "keep every file operation inside the current working directory",
                    "source": "deterministic",
                }

        if decision["outcome"] == "safe":
            return self._finish(decision, probability=0.0, extra={"source": "deterministic"})

        if decision["outcome"] == "refuse":
            return self._finish(decision, probability=1.0)

        # grey zone -> second opinion
        judgement = self._second_opinion(command)
        if judgement is None:
            decision.update({"outcome": "allow", "probability": None})
            if self.mode == "enforce":
                decision["reason"] = (
                    "judge unavailable; command matches no deterministic rule, allowed under the "
                    "documented fail-soft posture (degradation recorded)"
                )
            return self._finish(decision, probability=None, extra={"degraded": True})

        risk = judgement["risk_probability"]
        label = judgement["label"]
        decision |= {"judge": judgement, "source": judgement["source"]}
        if label in ("destructive", "out_of_scope"):
            if risk >= self.refuse_threshold:
                decision.update({"outcome": "refuse", "reason": f"judge verdict {label} (p={risk:.3f}): {judgement.get('reason', '')}",
                                 "suggestion": judgement.get("suggestion", "choose a command confined to the repository")})
            elif risk >= self.ask_threshold:
                if self.unattended:
                    decision.update({"outcome": "refuse",
                                     "reason": f"judge verdict {label} (p={risk:.3f}) needs confirmation and this run is unattended: {judgement.get('reason', '')}",
                                     "suggestion": judgement.get("suggestion", "prefer a read-only or in-workspace alternative")})
                else:
                    decision.update({"outcome": "ask", "reason": f"judge verdict {label} (p={risk:.3f})"})
            else:
                decision.update({"outcome": "allow", "reason": f"judge verdict {label} below threshold (p={risk:.3f})"})
        else:
            decision.update({"outcome": "allow", "reason": f"judge verdict safe (p_risk={risk:.3f})"})
        return self._finish(decision, probability=risk)

    # ------------------------------------------------------------- internals
    def _second_opinion(self, command: str) -> dict | None:
        if not (self.judge_enabled and self.judge is not None and getattr(self.judge, "available", False)):
            return None
        try:
            from harness.guardrails.judgments import action_questions

            state = self._compact_state(command)
            result = self.judge.judge_batch([state], action_questions())[0]
        except Exception as exc:  # a judge failure must never block execution
            if self.telemetry is not None:
                self.telemetry.degrade("policy_judge", f"{type(exc).__name__}: {exc}")
            return None
        if result is None or result.get("source") in ("unavailable", None):
            return None
        answers = result.get("answers") or {}
        from harness.laya.adapter import risk_from_action_answers
        from harness.laya.calibration import calibrate_binary

        label, raw_risk = risk_from_action_answers(answers)
        if label == "unknown":
            return None
        risk, temperature = calibrate_binary(getattr(self.judge, "calibration", None), "action_risk", raw_risk)
        severity = (answers.get("severity") or {}).get("score")
        return {
            "label": label,
            "risk_probability": risk,
            "risk_probability_raw": raw_risk,
            "temperature": temperature,
            "severity": severity,
            "source": result.get("source", "judge"),
            "latency_ms": result.get("latency_ms"),
            "answer_confidence": (answers.get("verdict") or {}).get("answer_confidence"),
            "reason": f"verdict {label}; P(risky) {raw_risk:.3f} -> {risk:.3f} (T={temperature})",
        }

    def _compact_state(self, command: str) -> str:
        """Compact state for the judge (never the whole conversation).

        Budget: the English checkpoint reads ~320 state tokens; we cap by characters
        (4 chars ≈ 1 token) and keep the newest, most decision-relevant part.
        """
        task = getattr(self.judge, "task_excerpt", "") or ""
        command = command[: self.max_command_chars]
        workspace = str(self.workspace)
        state = (
            f"Harness task: {task[:200]}\n"
            f"Working directory (workspace): {workspace}\n"
            f"Shell command to classify:\n{command}"
        )
        max_chars = int(getattr(self.judge, "max_state_chars", 1100))
        if len(state) > max_chars:
            state = state[:max_chars]
        return state

    def _finish(self, decision: dict, *, probability: float | None, extra: dict | None = None) -> dict:
        outcome = decision.get("outcome", "allow")
        if outcome == "safe":  # internal classification -> external outcome
            outcome = "allow"
        out = {
            "outcome": outcome,
            "rule": decision.get("rule", ""),
            "reason": decision.get("reason", ""),
            "suggestion": decision.get("suggestion", ""),
            "source": decision.get("source", "deterministic"),
            "probability": probability,
            "mode": self.mode,
        }
        if decision.get("judge"):
            judge = decision["judge"]
            out["judge"] = judge
            out["probability_raw"] = judge.get("risk_probability_raw")
            out["temperature"] = judge.get("temperature")
        out.update(extra or {})
        if self.telemetry is not None:
            self.telemetry.emit(
                "guardrail",
                outcome=out["outcome"],
                rule=out["rule"],
                source=out["source"],
                probability=out["probability"],
                probability_raw=out.get("probability_raw"),
                temperature=out.get("temperature"),
                mode=self.mode,
                reason=out["reason"][:300],
                command=(decision.get("segment") or "")[:300],
            )
        # shadow mode observes only: never block.
        if self.mode == "shadow" and out["outcome"] == "refuse":
            out["outcome"] = "allow"
            out["shadow_refusal"] = True
        if self.mode == "off":
            out["outcome"] = "allow"
        return out
