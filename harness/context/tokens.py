"""
Token accounting for the context-split panel (the /context-style breakdown).

Why it exists: "how much of the window is system prompt, how much is the
conversation, and what did compaction buy us?" must be visible live (TUI) and in
the report. This module is the single source of that arithmetic.

Counting method: tiktoken's ``cl100k_base`` is used as a *consistent
approximation* for every model — DeepSeek and Qwen tokenize differently, so the
absolute numbers are estimates. They are exact for the purpose of a *relative*
breakdown (which section dominates, does utilization grow, how much did
compaction prune). When tiktoken is unavailable (optional dependency) or its BPE
file cannot be fetched offline, the fallback is the standard chars/4 heuristic;
the method is recorded in every event so nobody mistakes an estimate for billed
tokens.

The "tools" section: mini-swe-agent drives a text command interface, so requests
carry no function schemas by default. The tokens spent documenting the harness's
own command surface (the sentinel block in the task prompt, extracted by
heading) are reported there instead; if a request *does* carry API tool schemas
(``tools=`` kwarg), those are counted as well. The section is always subtracted
from the message it appears in, so system + tools + messages == total.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

#: Heading that introduces the sentinel/tool surface in our instance prompt.
TOOLS_HEADING = "## Harness commands (sentinels)"

_CHARS_PER_TOKEN = 4
_ENCODER: Any = None
_ENCODER_TRIED = False


def _encoder():
    """Lazily build the cl100k encoder; None when tiktoken is unusable."""
    global _ENCODER, _ENCODER_TRIED
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # optional dependency, offline BPE fetch, ...
            _ENCODER = None
    return _ENCODER


def method() -> str:
    """Human-readable name of the active counting method."""
    return "tiktoken:cl100k_base" if _encoder() is not None else "chars/4 (tiktoken unavailable)"


def count_tokens(text: str) -> int:
    """Approximate token count for one string."""
    text = text or ""
    if not text:
        return 0
    encoder = _encoder()
    if encoder is not None:
        try:
            # disallowed_special=() keeps prompt text that happens to contain
            # "<|endoftext|>" from raising instead of counting.
            return len(encoder.encode(text, disallowed_special=()))
        except Exception:
            pass
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # OpenAI-style content arrays: keep the text parts only.
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def heading_section(text: str, heading: str = TOOLS_HEADING) -> str:
    """Return the ``heading`` section of ``text`` (up to the next ``##``/EOF)."""
    if not text or heading not in text:
        return ""
    match = re.search(rf"^{re.escape(heading)}\s*$.*?(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(0) if match else ""


def tools_schema_tokens(tools: Any) -> int:
    """Tokens of the API function schemas, when a request carries them."""
    if not tools:
        return 0
    try:
        return count_tokens(json.dumps(tools, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def bash_schema_tokens() -> int:
    """Tokens of upstream's bash tool schema — sent on every litellm request.

    Imported lazily from the vendored core so this module stays importable
    (and the report path stays working) when mini-swe-agent is absent.
    """
    try:
        from minisweagent.models.utils.actions_toolcall import BASH_TOOL

        return tools_schema_tokens([BASH_TOOL])
    except Exception:
        return 0


def split_context(
    messages: Iterable[dict],
    *,
    limit: int = 0,
    tools_tokens: int = 0,
    tools_heading: str = TOOLS_HEADING,
) -> dict:
    """Split the prompt into system / tools / messages / free token buckets.

    ``system + tools + messages == total``; ``free`` is derived from ``limit``.
    The returned dict is what the telemetry ``context_split`` field carries.
    """
    system = 0
    conversation = 0
    documented_tools = 0
    for message in messages or []:
        text = _message_text(message)
        if not text:
            continue
        section = heading_section(text, tools_heading)
        section_tokens = count_tokens(section) if section else 0
        body_tokens = max(0, count_tokens(text) - section_tokens)
        documented_tools += section_tokens
        if message.get("role") == "system":
            system += body_tokens
        else:
            conversation += body_tokens

    tools = documented_tools + max(0, int(tools_tokens or 0))
    total = system + tools + conversation
    limit = max(0, int(limit or 0))
    return {
        "system": system,
        "tools": tools,
        "messages": conversation,
        "total": total,
        "limit": limit,
        "utilization": round(total / limit, 4) if limit else 0.0,
        "method": method(),
    }
