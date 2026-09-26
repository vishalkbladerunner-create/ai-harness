"""
Brand theme — one place for the palette, the Neuromancer wordmark and the header.

Why it exists: the CLI, the TUI and the Markdown report must read as one brand
system. Every consumer imports the colours and the logo from here instead of
hard-coding hex values, so re-branding is a one-file change. Green/red are
reserved for pass/fail semantics only; everything else is navy/blue/white.

Team: Neuromancer (named after the William Gibson novel). The wordmark is a
two-line half-block banner: bright blue on top, navy below — the same two-tone
as the original shield mark, and narrow enough (47 columns) for an 80-column
terminal and for the report header.

No Rich import at module level: the report path (`banner_for_report`) must keep
working even when the optional TUI dependencies are absent. `header_panel`
imports Rich lazily.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Palette (sampled from the team mark)
# ---------------------------------------------------------------------------
BRAND_DARK = "#013E75"    # navy — shield body, borders, panel titles
BRAND_BRIGHT = "#0179D0"  # bright blue — logo highlight, active elements
BRAND_WHITE = "#FFFFFF"   # page/panel background
BRAND_MID = "#5B8FD6"     # messages segment (context bar)
BRAND_TRACK = "#C9D4E0"   # free space / empty bar track
BRAND_PASS = "#1B8A4B"    # pass semantics only
BRAND_FAIL = "#C0392B"    # fail semantics only

#: Panel title for the startup header / report header.
BRAND_TITLE = "NEUROMANCER · guarded-mini"
BRAND_SUBTITLE = "guarded SWE agent · local calibrated decisions"

#: A quiet nod to the novel (TUI header only — the report stays factual).
BRAND_QUOTE = "the sky above the port was the color of television, tuned to a dead channel"

# ---------------------------------------------------------------------------
# Wordmark — "NEUROMANCER", two lines, half-block glyphs, 47 columns.
# ---------------------------------------------------------------------------
_LOGO_ROWS: tuple[str, ...] = (
    "█▄░█ █▀▀ █░█ █▀█ █▀█ █▀▄▀█ ▄▀█ █▄░█ █▀▀ █▀▀ █▀█",
    "█░▀█ ██▄ █▄█ █▀▄ █▄█ █░▀░█ █▀█ █░▀█ █▄▄ ██▄ █▀▄",
)

#: Rich markup version: bright blue top line, navy bottom line (the mark's
#: two-tone). Renderable by any Rich console; strip with `logo_plain()`.
ASCII_LOGO = "\n".join(
    f"[{BRAND_BRIGHT}]{row}[/]" if index == 0 else f"[{BRAND_DARK}]{row}[/]"
    for index, row in enumerate(_LOGO_ROWS)
)

_MARKUP_RE = re.compile(r"\[[^\[\]]*\]")


def logo_markup() -> str:
    """The wordmark as a Rich-markup string (colours applied)."""
    return ASCII_LOGO


def logo_plain() -> str:
    """The wordmark with markup stripped — for the Markdown report and logs."""
    return "\n".join(_MARKUP_RE.sub("", line) for line in ASCII_LOGO.splitlines())


def logo_lines() -> list[str]:
    """Plain wordmark lines (no trailing empty lines)."""
    return [line for line in logo_plain().splitlines() if line.strip()]


def header_panel(*, quote: bool = False, subtitle: str = BRAND_SUBTITLE):
    """The branded startup header as a Rich Panel (lazy Rich import).

    Used by the CLI banner and the TUI. Returns ``None`` when Rich is missing so
    callers can fall back to plain text.
    """
    try:
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:  # pragma: no cover - Rich ships with the TUI extras
        return None

    body = Text.from_markup(logo_markup())
    if quote:
        body.append("\n\n")
        body.append(BRAND_QUOTE, style="italic dim")
    return Panel(
        body,
        title=f"[{BRAND_DARK} bold]{BRAND_TITLE}[/]",
        subtitle=f"[{BRAND_BRIGHT}]{subtitle}[/]",
        border_style=BRAND_BRIGHT,
        padding=(0, 2),
    )


def status_style(status: str) -> str:
    """Pass/fail colour for a run status; neutral otherwise (brand, not rainbow)."""
    if status in {"submitted", "pass", "ok", "submitted_ok"}:
        return BRAND_PASS
    if status in {"error", "fail", "limits_exceeded", "format_error", "timeout"}:
        return BRAND_FAIL
    return BRAND_BRIGHT
