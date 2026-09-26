#!/usr/bin/env python3
"""
Provider pre-flight — `make doctor`.

Why it exists: the evaluator exports only AI_API_KEY, and the harness discovers
at run time whether that key belongs to DeepSeek or Qwen. This script answers
the question *before* a run: it probes each checked-in provider with the key
(a cheap authenticated GET /models, no tokens billed; a 1-token completion only
when the endpoint does not support /models) and reports which provider accepts
the credential and whether the configured model IDs are advertised. Nothing is
written to disk and the key is never printed.

Exit codes: 0 = at least one provider accepts the key, 1 = none did,
2 = missing configuration (no AI_API_KEY).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.config import ConfigError, load_harness_config, provider_chain, read_env  # noqa: E402

TIMEOUT_SECONDS = 15


def _request(url: str, key: str, payload: dict | None = None) -> tuple[int, str]:
    """One HTTP call; returns (status, body). Network errors become (-1, reason)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = ""
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - a probe must never crash
        return -1, f"{type(exc).__name__}: {exc}"


def _advertised_ids(body: str) -> list[str] | None:
    """Model IDs from a /models payload, or None if the shape is unknown."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    return [entry.get("id", "") for entry in entries if isinstance(entry, dict)]


def _completion_probe(base_url: str, key: str, model_name: str) -> tuple[int, str]:
    """Fallback probe for endpoints without /models: one token, minimal cost."""
    return _request(
        f"{base_url.rstrip('/')}/chat/completions",
        key,
        {"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
    )


def main() -> int:
    try:
        env = read_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    key = env["AI_API_KEY"]
    try:
        chain = provider_chain(load_harness_config(), env)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    explicit = bool((env.get("MODEL_BASE_URL") or "").strip() and (env.get("MODEL_NAME") or "").strip())
    print("guarded-mini doctor — probing the provider chain with your AI_API_KEY")
    if explicit:
        print("(explicit MODEL_BASE_URL + MODEL_NAME are set: probing exactly that endpoint)")
    print()

    # Group the chain by base URL, preserving order: one auth probe per provider.
    by_base: dict[str, list[str]] = {}
    for entry in chain:
        by_base.setdefault(entry["base_url"], []).append(entry["model_name"])

    accepted: str | None = None
    for base_url, model_names in by_base.items():
        print(f"• {base_url}")
        status, body = _request(f"{base_url.rstrip('/')}/models", key)
        if status in (401, 403):
            print("    key: REJECTED (authentication failed)")
            continue
        if status == 404:
            # The endpoint has no /models; fall back to a 1-token completion.
            status, body = _completion_probe(base_url, key, model_names[0])
            if status in (401, 403):
                print("    key: REJECTED (authentication failed)")
                continue
            if status != 200:
                print(f"    unreachable or error (HTTP {status}): {body[:160]}")
                continue
            print(f"    key: accepted (verified with a 1-token {model_names[0]} completion; /models unsupported)")
        elif status != 200:
            print(f"    unreachable or error (HTTP {status}): {body[:160]}")
            continue
        else:
            print("    key: accepted")
        ids = _advertised_ids(body)
        for name in model_names:
            if ids is None:
                print(f"    model {name}: (advertised list unavailable — run will fall back if rejected)")
            elif name in ids:
                print(f"    model {name}: advertised")
            else:
                print(f"    model {name}: NOT advertised by this endpoint — update provider_defaults or export MODEL_NAME")
        if accepted is None:
            accepted = base_url

    print()
    if accepted is not None:
        print(f"OK: your key works with {accepted}")
        print("make run will use the first working provider automatically (DeepSeek first, then Qwen).")
        return 0
    print("FAIL: no provider accepted the credential.")
    print("guarded-mini works only with the official DeepSeek API (https://api.deepseek.com)")
    print("and the official Qwen API (Alibaba DashScope, https://dashscope.aliyuncs.com/compatible-mode/v1).")
    print("Check that AI_API_KEY is a valid key for one of them.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
