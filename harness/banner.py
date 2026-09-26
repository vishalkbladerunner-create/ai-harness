"""
CLI banner — small, functional branding (Phase 5.1).

Why it exists: the evaluation report is the artefact faculty and judges look at;
a consistent banner in the CLI and in the report header makes runs identifiable
and states the pinned upstream provenance at a glance.

Hackathon criteria served: Phase 5 persona + presentation (kept cheap).
"""

from __future__ import annotations

from harness import HARNESS_NAME, HARNESS_TAGLINE, UPSTREAM_COMMIT, UPSTREAM_NAME, UPSTREAM_VERSION, __version__

_BANNER = r"""
   ___                     _     _        _           _        _
  / __|_  _ __ _ _ _ __| |___| |    | \/ (_)_ _  __| |_  _ __| |
 | (_ | || / _` | '_/ _` / -_) |    | |\/| | ' \/ _` | || / _` |
  \___|\_,_\__,_|_| \__,_\___|_|    |_|  |_|_||_\__,_|\_,_\__,_|
"""


def banner_lines() -> list[str]:
    return [line for line in _BANNER.strip("\n").splitlines() if line.strip()]


def banner_text() -> str:
    return "\n".join(banner_lines())


def banner_for_report() -> str:
    return "\n".join(f"> `{line}`" for line in banner_lines())


def provenance_line() -> str:
    return (
        f"{HARNESS_NAME} v{__version__} — {HARNESS_TAGLINE} | "
        f"built on {UPSTREAM_NAME} v{UPSTREAM_VERSION} ({UPSTREAM_COMMIT[:12]}, MIT)"
    )


def print_banner(stream=None) -> None:
    import sys

    stream = stream or sys.stdout
    print(banner_text(), file=stream)
    print(f"  {provenance_line()}", file=stream)
    print(file=stream)
