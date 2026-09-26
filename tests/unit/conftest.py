"""Shared test configuration: repo-root import path + hermetic defaults."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keep any accidental upstream import project-local during tests.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(REPO_ROOT / ".cache" / "mini-swe-agent"))
os.environ.setdefault("HF_HOME", str(REPO_ROOT / ".cache" / "huggingface"))
