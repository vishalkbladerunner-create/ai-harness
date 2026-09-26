"""
guarded-mini — a minimal, guarded SWE harness with a local calibrated decision layer.

Built on mini-swe-agent (MIT, (c) the SWE-agent team), pinned and vendored under
``vendor/mini-swe-agent``. Everything in this top-level ``harness/`` package is ours;
the vendored upstream core is never modified.

Hackathon criterion served: the "what did YOU build?" question has a one-word
answer — ``harness/``.
"""

__version__ = "0.1.0"

import os as _os
from pathlib import Path as _Path

# ---------------------------------------------------------------------------
# Bootstrap (runs before ANY upstream mini-swe-agent import: importing any
# harness.* submodule executes this package __init__ first).
#
# Upstream, at import time, creates and dotenv-loads a *home-directory* config
# dir (`platformdirs.user_config_dir("mini-swe-agent")`). Compliance rule 5 says
# the harness must not depend on the home directory or pre-existing ~/.config,
# and loading a HOME .env could inject credentials we never saw. Both are
# neutralised here with upstream's documented env overrides — no code modified.
# ---------------------------------------------------------------------------
_REPO_ROOT = _Path(__file__).resolve().parents[1]
_os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")  # our banner replaces upstream's
_os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(_REPO_ROOT / ".cache" / "mini-swe-agent"))
_os.environ.setdefault("HF_HOME", str(_REPO_ROOT / ".cache" / "huggingface"))
_os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Retry cap with upstream's exponential backoff (Phase 1.4: retry caps + backoff).
_os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "3")

HARNESS_NAME = "guarded-mini"
HARNESS_TAGLINE = "minimal, guarded SWE harness with a local calibrated decision layer"
UPSTREAM_NAME = "mini-swe-agent"
UPSTREAM_VERSION = "2.4.6"
UPSTREAM_COMMIT = "a83fcae82d2a08f0ee0c688f9d137b3566c097f8"

__all__ = [
    "__version__",
    "HARNESS_NAME",
    "HARNESS_TAGLINE",
    "UPSTREAM_NAME",
    "UPSTREAM_VERSION",
    "UPSTREAM_COMMIT",
]
