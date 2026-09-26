#!/usr/bin/env python3
"""
Setup step: install the optional laya sidecar (NON-FATAL).

Why it exists: laya powers the calibrated decision layer, but the evaluation
environment may lack the ~800MB checkpoint or the disk/time to install torch.
Per the build rules, setup must skip + note + run degraded rather than fail.

Hackathon criteria served: compliance 1 (`make setup` must not fail), Phase 1/2
fallback-first design, laya sidecar packaging.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / ".cache"
STATUS_PATH = CACHE_DIR / "laya-setup.json"
CONSTRAINTS = REPO_ROOT / "constraints.txt"
INSTALL_TIMEOUT_S = int(os.environ.get("LAYA_INSTALL_TIMEOUT", "1800"))


def _install_cmd(constrained: bool) -> list[str]:
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
    if constrained and CONSTRAINTS.exists():
        cmd += ["-c", str(CONSTRAINTS)]
    return cmd + ["laya[serve]"]


def main() -> int:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    status = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "installed": False}
    cmd = _install_cmd(constrained=True)
    print(f"[laya-setup] {' '.join(cmd)} (timeout {INSTALL_TIMEOUT_S}s, non-fatal)")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S)
        if proc.returncode != 0 and CONSTRAINTS.exists():
            print("[laya-setup] constrained install failed; retrying unpinned (non-fatal either way)")
            proc = subprocess.run(_install_cmd(constrained=False), capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S)
        status["returncode"] = proc.returncode
        status["tail"] = (proc.stderr or proc.stdout or "")[-2000:]
        if proc.returncode != 0:
            status["reason"] = "pip install laya failed (see tail)"
            print("[laya-setup] SKIPPED: pip install failed; harness will run in degraded mode.")
        else:
            status["installed"] = True
            print("[laya-setup] laya installed.")
    except subprocess.TimeoutExpired:
        status["reason"] = f"pip install timed out after {INSTALL_TIMEOUT_S}s"
        print("[laya-setup] SKIPPED: install timed out; harness will run in degraded mode.")
    except Exception as exc:  # never fail setup
        status["reason"] = f"{type(exc).__name__}: {exc}"
        print(f"[laya-setup] SKIPPED: {exc}; harness will run in degraded mode.")

    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(f"[laya-setup] status written to {STATUS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
