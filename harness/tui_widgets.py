"""
Rich renderables for the harness TUI (and any terminal consumer of the stream).

Why it exists: ``harness/tui.py`` is a thin Textual shell; everything that
*renders* lives here as pure functions over telemetry data, so the context panel
and the event lines can be unit-tested without a terminal and reused by future
views (HTML report, dashboard) that want the same numbers.

Brand rules: navy/blue/white from ``harness/theme.py``; green/red appear only
where the value is literally pass/fail (verification, run status, refusals).
"""

from __future__ import annotations

from rich.console import Group
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from harness.theme import (
    BRAND_BORDER,
    BRAND_BRIGHT,
    BRAND_DARK,
    BRAND_FAIL,
    BRAND_MID,
    BRAND_PASS,
    BRAND_SKY,
    BRAND_TEXT,
    BRAND_TRACK,
    BRAND_WHITE,
    status_style,
)

BAR_WIDTH = 28
#: name, colour, what the row means (kept visible in the caption). Colours are
#: the text-safe set: they must stay readable on a dark terminal.
SECTIONS = (
    ("system", BRAND_BORDER, "system prompt"),
    ("tools", BRAND_SKY, "bash schema + sentinel docs"),
    ("messages", BRAND_MID, "conversation"),
    ("free", BRAND_TRACK, "free window"),
)


def _bar(fraction: float, color: str) -> ProgressBar:
    """A single-segment bar. Tiny non-zero values stay visible (min sliver)."""
    fraction = min(max(float(fraction or 0.0), 0.0), 1.0)
    if 0.0 < fraction < 0.004:
        fraction = 0.004
    return ProgressBar(
        total=1.0,
        completed=fraction,
        width=BAR_WIDTH,
        complete_style=color,
        finished_style=color,
        style=BRAND_TRACK,
    )


def context_split_panel(
    split: dict | None,
    *,
    limit: int | None = None,
    compaction_saved: int = 0,
    compaction_passes: int = 0,
    title: str = "Context",
):
    """The /context-style breakdown: system / tools / messages / free.

    ``split`` is a ``context_split`` payload from telemetry (may be None before
    the first model call). ``limit`` overrides the payload's own limit (used by
    the TUI so the panel has a scale before any call arrives).
    """
    split = split or {}
    limit = int(limit if limit is not None else split.get("limit") or 0)
    total = int(split.get("total") or 0)
    utilization = split.get("utilization")
    if utilization is None:
        utilization = (total / limit) if limit else 0.0

    if limit:
        heading = f"{title}  {total:,}/{limit:,}  ({float(utilization):.0%})"
    else:
        heading = f"{title}  {total:,} tok  (window unknown)"

    table = Table(title=heading, title_style=f"bold {BRAND_SKY}", show_header=False, pad_edge=False)
    table.add_column("section", width=9)
    table.add_column("bar", width=BAR_WIDTH, no_wrap=True)
    table.add_column("tokens", justify="right", width=9)

    for name, color, _note in SECTIONS:
        if name == "free":
            count = max(0, limit - total)
            fraction = (count / limit) if limit else 0.0
        else:
            count = int(split.get(name) or 0)
            fraction = (count / limit) if limit else 0.0
        label = Text(name, style=f"bold {color}" if name != "free" else f"bold {BRAND_TEXT}")
        table.add_row(label, _bar(fraction, color), Text(f"{count:,}", style="dim"))

    captions: list[Text] = []
    method = split.get("method") or "tiktoken:cl100k_base"
    legend = Text()
    legend.append("sections: ", style="dim")
    legend.append("system", style=BRAND_BORDER)
    legend.append(" · ", style="dim")
    legend.append("tools", style=BRAND_BRIGHT)
    legend.append(" (bash schema + sentinel docs) · ", style="dim")
    legend.append("messages", style=BRAND_MID)
    legend.append(f" · counting: {method} (approximation)", style="dim")
    captions.append(legend)

    saved = Text()
    if compaction_passes:
        saved.append("compaction saved ", style="dim")
        saved.append(f"{int(compaction_saved):,} tok", style=f"bold {BRAND_BRIGHT}")
        saved.append(f" over {compaction_passes} pass(es) — tokens pruned before sending", style="dim")
    else:
        saved.append("compaction saved 0 tok — no pruning pass yet", style="dim")
    captions.append(saved)

    return Group(table, *captions)


def _clip(value, limit: int = 96) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def event_line(event: dict) -> Text:
    """One brand-coloured line per telemetry event (the agent stream view)."""
    kind = str(event.get("kind") or "?")
    line = Text()
    line.append(f"{str(event.get('iso') or '')[11:19]} ", style="dim")

    if kind == "run_start":
        issue = event.get("issue") or {}
        line.append("run start ", style=f"bold {BRAND_SKY}")
        line.append(f"· {_clip(issue.get('title') or issue.get('source') or 'issue', 70)}", style="dim")
    elif kind == "run_end":
        status = str(event.get("status") or "?")
        stats = event.get("stats") or {}
        line.append("run end ", style=f"bold {BRAND_SKY}")
        line.append(status.upper(), style=f"bold {status_style(status)}")
        line.append(
            f" · steps {stats.get('steps', 0)} · tokens {int(stats.get('total_tokens') or 0):,}"
            f" · wall {stats.get('wall_seconds', 0)}s",
            style="dim",
        )
    elif kind == "model_call":
        usage = event.get("usage") or {}
        split = event.get("context_split") or {}
        line.append("model ", style=f"bold {BRAND_SKY}")
        line.append(f"{int(usage.get('total_tokens') or 0):,} tok", style=BRAND_BRIGHT)
        if split:
            line.append(
                f" · ctx {int(split.get('total') or 0):,}/{int(split.get('limit') or 0):,}"
                f" ({float(split.get('utilization') or 0):.0%})",
                style="dim",
            )
        line.append(f" · {event.get('latency_s', 0)}s", style="dim")
    elif kind == "step":
        actions = event.get("actions") or []
        line.append(f"step {event.get('step', '?')} ", style=f"bold {BRAND_BRIGHT}")
        line.append(_clip(actions[0] if actions else "", 84), style="")
    elif kind == "command":
        policy = event.get("policy") or {}
        decision = str(event.get("decision") or policy.get("outcome") or "?")
        color = {"allow": BRAND_TEXT, "refuse": BRAND_FAIL, "ask": BRAND_SKY}.get(decision, BRAND_TEXT)
        line.append(f"{decision:>6} ", style=f"bold {color}")
        line.append(_clip(event.get("command"), 76), style="")
        line.append(f" · rc {event.get('returncode', '?')} · {event.get('duration_s', 0)}s", style="dim")
    elif kind == "guardrail":
        outcome = str(event.get("outcome") or "?")
        color = {"allow": BRAND_TEXT, "refuse": BRAND_FAIL, "ask": BRAND_SKY}.get(outcome, BRAND_TEXT)
        line.append("guardrail ", style=f"bold {color}")
        line.append(outcome, style=color)
        probability = event.get("probability")
        line.append(
            f" · p={float(probability):.2f}" if probability is not None else " · deterministic",
            style="dim",
        )
        line.append(f" · {_clip(event.get('reason'), 60)}", style="dim")
    elif kind == "verification":
        ok = bool(event.get("ok"))
        line.append("verify ", style=f"bold {BRAND_SKY}")
        line.append("PASS" if ok else "FAIL", style=f"bold {BRAND_PASS if ok else BRAND_FAIL}")
        line.append(f" · {_clip(event.get('command'), 50)} · {event.get('duration_s', 0)}s", style="dim")
    elif kind == "compaction":
        shadow = bool(event.get("shadow"))
        line.append("compaction ", style=f"bold {BRAND_SKY}")
        line.append("shadow" if shadow else "live", style=BRAND_BRIGHT)
        line.append(
            f" · {int(event.get('tokens_before') or 0):,}→{int(event.get('tokens_after') or 0):,} tok"
            f" · saved {int(event.get('tokens_saved') or 0):,}"
            f" · dropped {len(event.get('dropped') or [])}",
            style="dim",
        )
    elif kind == "injection_scan":
        flagged = bool(event.get("flagged"))
        line.append("injection scan ", style=f"bold {BRAND_SKY}")
        line.append("FLAGGED" if flagged else "clean", style=f"bold {BRAND_FAIL if flagged else BRAND_PASS}")
        line.append(f" · {_clip(event.get('where'), 60)}", style="dim")
    elif kind in {"degradation", "error", "budget_exhausted", "verification_failed_after_submit"}:
        line.append(kind.replace("_", " ") + " ", style=f"bold {BRAND_FAIL}")
        detail = event.get("reason") or event.get("error") or event.get("command") or ""
        line.append(_clip(detail, 90), style="dim")
    elif kind == "provider_fallback":
        line.append("provider fallback ", style=f"bold {BRAND_BRIGHT}")
        line.append(f"{event.get('previous_model')} → {event.get('model')}", style="dim")
    elif kind == "workspace_changes":
        line.append("workspace changes ", style=f"bold {BRAND_SKY}")
        line.append(_clip(event.get("note"), 70), style="dim")
    elif kind == "submit_patch":
        line.append("submit patch ", style=f"bold {BRAND_PASS}")
        line.append(f"{event.get('bytes', 0):,} bytes", style="dim")
    else:
        line.append(kind.replace("_", " ") + " ", style=f"bold {BRAND_SKY}")
        picked = " ".join(
            _clip(value, 40)
            for key, value in event.items()
            if key not in {"ts", "iso", "run_id", "kind"} and isinstance(value, (str, int, float, bool))
        )
        line.append(_clip(picked, 90), style="dim")
    return line


def status_line(state: dict) -> Text:
    """The one-line status bar under the panels (brand palette only)."""
    line = Text()
    line.append(" guarded-mini ", style=f"bold {BRAND_WHITE} on {BRAND_DARK}")
    status = str(state.get("status") or "starting")
    line.append(f" {status} ", style=f"bold {status_style(status)} on {BRAND_TRACK}")
    parts = []
    if state.get("steps") is not None:
        max_steps = state.get("max_steps") or 0
        parts.append(f"step {state['steps']}" + (f"/{max_steps}" if max_steps else ""))
    if state.get("tokens") is not None:
        token_part = f"tok {int(state['tokens']):,}"
        detail = []
        if state.get("tokens_in"):
            detail.append(f"in {int(state['tokens_in']):,}")
        if state.get("tokens_cache"):
            detail.append(f"cache {int(state['tokens_cache']):,}")
        if state.get("tokens_out"):
            detail.append(f"out {int(state['tokens_out']):,}")
        parts.append(token_part + (f" ({' · '.join(detail)})" if detail else ""))
    if state.get("wall_seconds") is not None:
        parts.append(f"wall {state['wall_seconds']}s")
    if state.get("model"):
        parts.append(str(state["model"]))
    if state.get("run_id"):
        parts.append(f"run {state['run_id']}")
    if state.get("mode"):
        parts.append(str(state["mode"]))
    line.append("  " + " · ".join(parts) + " ", style=f"{BRAND_DARK} on {BRAND_TRACK}")
    return line


def repo_graph(root_label: str, files: list[str], changed: set[str], *, max_nodes: int = 400) -> Tree:
    """Directory tree with changed (just-touched) files highlighted.

    Pure function over a file list so it is testable; the caller gets the list
    from git (see ``harness/tui.py``).
    """
    tree = Tree(f"[bold {BRAND_SKY}]{root_label or 'workspace'}[/]")
    directories: dict[tuple[str, ...], Tree] = {}
    for relative in files[:max_nodes]:
        parts = [part for part in relative.split("/") if part]
        if not parts:
            continue
        parent = tree
        key: tuple[str, ...] = ()
        for part in parts[:-1]:
            key += (part,)
            if key not in directories:
                directories[key] = parent.add(f"[{BRAND_MID}]{part}/[/]")
            parent = directories[key]
        if relative in changed:
            parent.add(f"[bold {BRAND_SKY}]{parts[-1]}[/]", style="reverse")
        else:
            parent.add(parts[-1])
    if not files:
        tree.add("[dim](no files)[/]")
    return tree
