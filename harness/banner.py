"""
CLI banner — Neuromancer branding for the CLI and the report header (Phase 5.1).

Why it exists: the evaluation report is the artefact faculty and judges look at;
a consistent banner in the CLI and in the report header makes runs identifiable
and states the pinned upstream provenance at a glance. The wordmark and palette
live in ``harness/theme.py`` — this module only decides where they appear.

Hackathon criteria served: Phase 5 persona + presentation (kept cheap).
"""

from __future__ import annotations

from harness import HARNESS_NAME, HARNESS_TAGLINE, UPSTREAM_COMMIT, UPSTREAM_NAME, UPSTREAM_VERSION, __version__
from harness.theme import header_panel, logo_lines, logo_plain


def banner_lines() -> list[str]:
    return logo_lines()


def banner_text() -> str:
    return logo_plain()


def banner_for_report() -> str:
    return "\n".join(f"> `{line}`" for line in banner_lines())


def provenance_line() -> str:
    return (
        f"{HARNESS_NAME} v{__version__} — {HARNESS_TAGLINE} | "
        f"built on {UPSTREAM_NAME} v{UPSTREAM_VERSION} ({UPSTREAM_COMMIT[:12]}, MIT)"
    )


def print_banner(stream=None, *, quote: bool = True) -> None:
    """Print the branded header. Falls back to plain text without Rich."""
    import sys

    stream = stream or sys.stdout
    panel = header_panel(quote=quote)
    if panel is not None:
        try:
            from rich.console import Console

            console = Console(file=stream, highlight=False)
            console.print(panel)
            console.print(f"  [dim]{provenance_line()}[/]")
            console.print()
            return
        except Exception:  # pragma: no cover - never let branding break a run
            pass
    print(banner_text(), file=stream)
    print(f"  {provenance_line()}", file=stream)
    print(file=stream)
