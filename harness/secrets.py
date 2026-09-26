"""
Secret hygiene utilities — one home for masking and credential detection.

Why it exists: the evaluation environment hands us a single credential
(``AI_API_KEY``) plus endpoint variables. Rule 6 of the hackathon forbids
credentials in any file; rule 1 of the guardrails forbids echoing them. Every log
line, report, telemetry record and observation passes through this module so a
leaked secret can only happen by bypassing a single choke point.

Hackathon criteria served: no hard-coded credentials anywhere (compliance 6),
secret hygiene (Phase 1.5), readable reports that are safe to share.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Environment variable name fragments whose *values* must never be echoed.
SENSITIVE_NAME_FRAGMENTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

# Common credential shapes found in file contents (used by the commit guard and
# by masking). Kept deliberately conservative to avoid mangling real code.
CREDENTIAL_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-\./+=]{16,})"),
    re.compile(r"(?i)\bauthorization:\s*bearer\s+\S+"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),
)

REDACTED = "[REDACTED]"


def sensitive_env_values(env: dict[str, str] | None = None) -> list[str]:
    """Return values of environment variables whose names look credential-like.

    Values shorter than 8 chars are ignored (they produce absurd matches like "1").
    """
    env = env if env is not None else dict(os.environ)
    values = []
    for name, value in env.items():
        if not value or len(value) < 8:
            continue
        upper = name.upper()
        if any(fragment in upper for fragment in SENSITIVE_NAME_FRAGMENTS):
            values.append(value)
    # Longest first so that masking a secret that contains another one still works.
    return sorted(set(values), key=len, reverse=True)


def mask_text(text: str, extra_values: list[str] | None = None) -> str:
    """Replace credential-like strings in *text* with ``[REDACTED]``."""
    if not text:
        return text
    for value in sensitive_env_values():
        if value in text:
            text = text.replace(value, REDACTED)
    for value in extra_values or []:
        if value and value in text:
            text = text.replace(value, REDACTED)
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def contains_credential_like(text: str) -> bool:
    """True when *text* contains something shaped like a credential.

    Used before staging/committing files: we refuse rather than guess.
    """
    if not text:
        return False
    for value in sensitive_env_values():
        if value in text:
            return True
    return any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS)


def is_sensitive_name(name: str) -> bool:
    """True when a variable/field *name* looks credential-like."""
    upper = (name or "").upper()
    return any(fragment in upper for fragment in SENSITIVE_NAME_FRAGMENTS)


def mask_obj(value: Any) -> Any:
    """Recursively mask every string inside JSON-compatible data.

    The same choke point is used for telemetry, reports and the trajectory, so a
    serialized structure can never carry a credential-shaped string to disk.
    """
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_obj(v) for v in value]
    return value


def redact_sensitive_fields(value: Any) -> Any:
    """Replace string values whose *field name* is credential-like with [REDACTED].

    Belt to ``mask_text``'s braces: even a value that does not look like a key
    (e.g. an opaque token) is removed when it is stored under a key-like name
    such as ``api_key``. Non-string values (counts, flags) are left untouched.
    """
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            if isinstance(item, str) and item and is_sensitive_name(str(key)):
                out[key] = REDACTED
            else:
                out[key] = redact_sensitive_fields(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_fields(item) for item in value]
    return value
