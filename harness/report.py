"""
End-of-run Markdown report — THE evaluation artefact (Phase 4.2).

Why it exists: judges, evaluators and the faculty round read the report, not the
JSONL. Every claim the harness makes (guardrail fired with probability p,
compaction dropped n items, tests passed) must be visible here with its
provenance. ``render_report`` is a pure function of a context dict so it can be
unit-tested without running an agent.

Hackathon criteria served: Phase 4.2 (report contents), Phase 2.5 (compaction
audit before/after), Phase 3.1 (test evidence), Phase 1.1 (guardrail log).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.banner import banner_for_report, provenance_line
from harness.secrets import mask_text


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _code_block(text: str, limit: int = 4000) -> str:
    text = text or ""
    if len(text) > limit:
        text = text[: limit // 2] + "\n...\n" + text[-limit // 2 :]
    return f"```\n{text}\n```"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return "_(none)_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell).replace("\n", " ").replace("|", "\\|") for cell in row) + " |")
    return "\n".join(out) + "\n"


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}\n\n"


def _context_section(context: dict) -> str:
    """The /context-style split + what compaction pruned (token-efficiency evidence)."""
    split = context.get("last_split") or {}
    if not split:
        return "No model calls recorded; the context split is unavailable for this run.\n"
    limit = int(split.get("limit") or context.get("limit") or 0)
    total = int(split.get("total") or 0)
    utilization = split.get("utilization")
    if utilization is None:
        utilization = (total / limit) if limit else 0.0

    def row(name: str, count: int) -> list[str]:
        share = f"{count / limit:.1%}" if limit else "n/a"
        return [name, _fmt_int(count), share]

    rows = [
        row("system", int(split.get("system") or 0)),
        row("tools", int(split.get("tools") or 0)),
        row("messages", int(split.get("messages") or 0)),
        row("total (last call)", total),
        row("free", max(0, limit - total)),
    ]
    passes = int(context.get("compaction_passes") or 0)
    saved = int(context.get("tokens_saved") or 0)
    body = (
        f"Last request: **{_fmt_int(total)}/{_fmt_int(limit)} tokens ({utilization:.0%} of the window)**. "
        f"Counting: `{split.get('method', 'approximate')}` — an approximation for DeepSeek/Qwen tokenizers, "
        "used for the relative breakdown.\n\n"
        + _table(rows, ["section", "tokens", "share of window"])
        + "\n"
        + (
            f"Compaction pruned **{_fmt_int(saved)} tokens** across {passes} pass(es) before they were sent "
            "(tokens that would otherwise have been in the prompt; see the Compaction audit for per-item verdicts).\n"
            if passes
            else "No live compaction pass ran in this run.\n"
        )
    )
    return body


def render_report(ctx: dict) -> str:
    """Render the Markdown report from a context dict produced by ``run.py``."""
    status = ctx.get("status", "unknown")
    ok_marker = {"submitted": "SUBMITTED", "limits_exceeded": "BUDGET EXHAUSTED", "error": "ERROR"}.get(status, status.upper())
    parts: list[str] = []

    parts.append(
        f"""# guarded-mini run report

{banner_for_report()}

**{provenance_line()}**

| field | value |
|---|---|
| run id | `{ctx.get("run_id", "")}` |
| status | **{ok_marker}** |
| exit status | `{ctx.get("exit_status", "")}` |
| workspace | `{ctx.get("workspace", "")}` |
| model | `{ctx.get("model_name", "")}` |
| started | {ctx.get("started_iso", "")} |
| duration | {ctx.get("duration_s", 0)}s |

"""
    )

    issue = ctx.get("issue") or {}
    parts.append(
        _section(
            "Issue",
            f"Source: `{issue.get('source', '')}` — {issue.get('chars', 0)} characters\n\n"
            + f"Parsed scope hints: `{json.dumps({k: issue.get(k) for k in ('paths', 'identifiers', 'tests')}, ensure_ascii=False)}`\n\n"
            + _code_block(issue.get("text", ""), 3000),
        )
    )

    stats = ctx.get("stats") or {}
    cost_total = float(stats.get("cost_total") or 0.0)
    parts.append(
        _section(
            "Summary",
            _table(
                [
                    ["steps", _fmt_int(stats.get("steps", 0))],
                    ["model calls", _fmt_int(stats.get("llm_calls", 0))],
                    ["prompt tokens", _fmt_int(stats.get("prompt_tokens", 0))],
                    ["completion tokens", _fmt_int(stats.get("completion_tokens", 0))],
                    ["total tokens", _fmt_int(stats.get("total_tokens", 0))],
                    ["provider-reported cost", f"${cost_total:.4f}" if cost_total > 0 else "not reported (unknown price table)"],
                    ["token accounting", "estimated (endpoint omitted usage)" if stats.get("estimated_usage") else "provider-reported"],
                    ["wall clock", f"{stats.get('wall_seconds', 0)}s"],
                    ["verification runs", _fmt_int(stats.get("verification_runs", 0))],
                    ["patch size", f"{_fmt_int(stats.get('patch_bytes', 0))} bytes"],
                ],
                ["metric", "value"],
            ),
        )
    )

    context = ctx.get("context") or {}
    parts.append(_section("Context window", _context_section(context)))

    error = ctx.get("error") or ""
    if error:
        parts.append(_section("Error", _code_block(error, 2000)))

    repro = ctx.get("reproducibility") or {}
    if repro:
        parts.append(
            _section(
                "Reproducibility",
                _table(
                    [
                        ["model", repro.get("model", "")],
                        ["temperature (pinned)", repro.get("temperature")],
                        ["seed (pinned)", repro.get("seed")],
                        ["upstream core", repro.get("upstream", "")],
                        ["judge calibration", repro.get("calibration", "")],
                    ],
                    ["setting", "value"],
                ),
            )
        )

    patch = ctx.get("patch") or {}
    changed = patch.get("files") or []
    parts.append(
        _section(
            "Files changed",
            _table([[f.get("status", ""), f"`{f.get('path', '')}`", f.get("additions", ""), f.get("deletions", "")] for f in changed],
                   ["status", "path", "+", "-"])
            + (f"\nPatch artefact: `{patch.get('path', '')}`\n\n" + _code_block(patch.get("diff", ""), 2500) if patch.get("diff") else "\n_(no patch captured)_\n"),
        )
    )

    verification = ctx.get("verification") or {}
    if verification:
        rows = []
        for record in verification.get("history", [])[-5:]:
            rows.append(["PASS" if record.get("ok") else "FAIL", record.get("reason", ""), str(record.get("returncode")), f"{record.get('duration_s', 0)}s"])
        body = f"Detected command: `{verification.get('command', '')}` (kind: {verification.get('kind', 'n/a')})\n\n"
        body += _table(rows, ["result", "trigger", "rc", "time"])
        last = verification.get("last") or {}
        if last:
            body += f"\nLast verification output:\n\n{_code_block(last.get('output_tail', ''), 2000)}"
        parts.append(_section("Verification evidence", body))
    else:
        parts.append(_section("Verification evidence", "_No test command detected; self-verification did not run._"))

    # Guardrails
    decisions = ctx.get("guardrail_decisions") or []
    g_rows = []
    for record in decisions[-30:]:
        g_rows.append(
            [
                record.get("outcome", ""),
                record.get("source", ""),
                f"{record['probability']:.3f}" if isinstance(record.get("probability"), (int, float)) else "",
                (record.get("rule", "") or "")[:40],
                (record.get("reason", "") or "")[:120],
                (record.get("command", "") or "")[:80],
            ]
        )
    guardrail_body = _table(g_rows, ["decision", "source", "p", "rule", "reason", "command"])
    armed = ctx.get("judge") or {}
    if decisions and all((row[1] in ("", "none") for row in g_rows)):
        guardrail_body += "\n_No policy object was armed in this run (Phase 0 plumbing)._\n"
    elif not decisions:
        guardrail_body += "\n_No commands were executed._\n"
    guardrail_body += f"\nDecision layer: {json.dumps(armed)}\n"
    parts.append(_section("Guardrail log", guardrail_body))

    # Scope
    scope = ctx.get("scope") or {}
    if scope:
        scope_rows = [
            [("in scope" if entry.get("in_scope") else "FLAGGED"), f"`{entry.get('path')}`", entry.get("source", ""), entry.get("reason", "")[:80]]
            for entry in (scope.get("files") or [])
        ]
        scope_body = _table(scope_rows, ["verdict", "path", "source", "reason"])
        if scope.get("rolled_back"):
            scope_body += "\nRolled back:\n\n" + _table(
                [[rb.get("path"), rb.get("action", "")] for rb in scope["rolled_back"]], ["path", "action"]
            )
        if scope.get("ignored"):
            scope_body += f"\nIgnored cache/artefact paths: {len(scope['ignored'])}\n"
        if scope.get("patch_exclusions"):
            scope_body += (
                "\nExcluded from the submitted patch (left in the workspace, flagged above): "
                + ", ".join(f"`{path}`" for path in scope["patch_exclusions"])
                + "\n"
            )
        scope_body += f"\nMode: `{scope.get('mode', '')}`\n"
        parts.append(_section("Scope check", scope_body))

    # Injection scans
    injection = ctx.get("injection") or {}
    if injection:
        scans = [
            ["FLAGGED" if s.get("flagged") else "clean", s.get("where", ""), ", ".join(s.get("matches") or []) or "-",
             s.get("judge_probability") if s.get("judge_probability") is not None else "-"]
            for s in (injection.get("scans") or [])[-15:]
        ]
        injection_body = _table(scans, ["verdict", "where", "deterministic matches", "judge P(injection)"])
        if not scans:
            injection_body += "\n_No untrusted text was scanned._\n"
        parts.append(_section("Injection defence", injection_body))

    # Compaction
    compaction = ctx.get("compaction") or {}
    if compaction:
        last = compaction.get("last") or {}
        compaction_body = _table(
            [
                ["mode", compaction.get("mode", "")],
                ["compaction runs", _fmt_int(compaction.get("runs", 0))],
                ["items judged (cumulative)", _fmt_int(compaction.get("items_judged", 0))],
                ["items dropped (last run)", _fmt_int(len(last.get("dropped") or []))],
                ["items shortened (last run)", _fmt_int(len(last.get("shortened") or []))],
                ["items rescued (staleness)", _fmt_int(compaction.get("rescued", 0))],
                ["tokens before -> after (last run)", f"{_fmt_int(last.get('tokens_before', 0))} -> {_fmt_int(last.get('tokens_after', 0))}"],
                ["tokens saved (cumulative est.)", _fmt_int(compaction.get("tokens_saved_estimate", 0))],
                ["judge source (last run)", last.get("judge_source", "-")],
            ],
            ["metric", "value"],
        )
        verdicts = last.get("verdicts") or []
        if verdicts:
            compaction_body += "\n" + _table(
                [
                    [
                        v.get("item", ""),
                        v.get("kind", ""),
                        v.get("verdict", ""),
                        f"{v['p_drop']:.2f}" if isinstance(v.get("p_drop"), (int, float)) else "-",
                        f"{v['p_essential']:.2f}" if isinstance(v.get("p_essential"), (int, float)) else "-",
                        f"{v['staleness']:.2f}" if isinstance(v.get("staleness"), (int, float)) else "-",
                        v.get("source", ""),
                        (v.get("reason", "") or "")[:60],
                    ]
                    for v in verdicts[:20]
                ],
                ["item", "kind", "verdict", "P(drop)", "P(essential)", "staleness", "source", "reason"],
            )
        if compaction.get("mode") == "shadow":
            compaction_body += "\n_Shadow mode: verdicts were judged and logged but nothing was pruned._\n"
        parts.append(_section("Compaction", compaction_body))
    else:
        parts.append(_section("Compaction", "_Compaction did not run._"))

    degradations = ctx.get("degradations") or []
    if degradations:
        parts.append(
            _section(
                "Degradation notes",
                _table([[d.get("component", ""), d.get("reason", "")[:200]] for d in degradations], ["component", "reason"]),
            )
        )
    else:
        parts.append(_section("Degradation notes", "_None: every layer ran in its primary mode._"))

    budget = ctx.get("budget") or {}

    def _budget_value(key: str, value) -> str:
        # 0 is the "no cap" convention for every max_* limit (see harness.yaml).
        if key.startswith("max_") and not value:
            return "unlimited"
        return str(value)

    parts.append(
        _section(
            "Budget",
            _table([[k, _budget_value(k, v)] for k, v in budget.items()], ["limit", "value"]),
        )
    )

    timeline = ctx.get("timeline") or []
    parts.append(
        _section(
            "Timeline (last events)",
            _table([[e.get("iso", ""), e.get("kind", ""), _event_summary(e)] for e in timeline[-20:]], ["time", "kind", "summary"]),
        )
    )

    appendix = ctx.get("calibration_appendix")
    if appendix:
        parts.append(_section("Calibration appendix (this run)", _code_block(json.dumps(appendix, indent=2), 4000)))

    parts.append(
        _section(
            "Submission",
            _code_block(ctx.get("submission", "") or "_(empty)_", 2000),
        )
    )
    parts.append(
        "\n---\n\n"
        f"Generated by guarded-mini. Core loop: mini-swe-agent (MIT, (c) SWE-agent team), commit "
        f"{ctx.get('upstream_commit', '')}. Telemetry: `{ctx.get('telemetry_path', '')}`.\n"
    )
    # Reports are shareable artefacts: mask credential-like strings at the very end.
    return mask_text("".join(parts))


def _event_summary(event: dict) -> str:
    kind = event.get("kind")
    if kind == "model_call":
        usage = event.get("usage") or {}
        return f"tokens={usage.get('total_tokens', '?')} latency={event.get('latency_s')}s"
    if kind == "command":
        return f"rc={event.get('returncode')} {str(event.get('command', ''))[:60]}"
    if kind == "step":
        return f"step={event.get('step')} actions={event.get('n_actions')}"
    if kind == "verification":
        return f"{'PASS' if event.get('ok') else 'FAIL'} {event.get('command', '')}"
    if kind == "degradation":
        return f"{event.get('component')}: {event.get('reason')}"
    if kind == "guardrail":
        return f"{event.get('outcome')} p={event.get('probability')}"
    if kind == "compaction":
        return f"dropped={event.get('dropped')} kept={event.get('kept')}"
    if kind == "run_end":
        return f"status={event.get('status')} exit={event.get('exit_status')}"
    return str(event.get("reason", ""))[:80]


def write_report(run_dir: Path, filename: str, ctx: dict) -> Path:
    path = Path(run_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(ctx), encoding="utf-8")
    return path
