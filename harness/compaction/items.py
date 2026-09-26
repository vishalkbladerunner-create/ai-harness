"""
Deterministic preparation for compaction — layer 1 of 3.

Why it exists: the judge must never see raw conversation state (window limit) and
the token maths must never be delegated to a classifier. This module turns the
canonical message list into *items* (causal call/result units), attaches pins for
high-consequence content, marks the recency exemption, and computes token
estimates that the fitting layer uses.

Hackathon criteria served: Phase 2.1/2.3 (deterministic prep, conservative pins,
never sever call/result pairs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from harness.secrets import contains_credential_like

#: Content that must survive compaction regardless of the judge.
PIN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("failing-test-evidence", re.compile(r'<verification[^>]*status="FAIL"', re.I)),
    ("quarantine-warning", re.compile(r"<quarantine", re.I)),
    ("task-reference", re.compile(r"harness_submit_patch|harness_load_issue")),
]

VERIFICATION_BLOCK = re.compile(r"<verification[^>]*>", re.I)


@dataclass
class Item:
    """A causally indivisible unit of conversation history."""

    id: str
    indices: list[int]  # positions in the canonical message list
    kind: str  # system | task | turn | notice | exit
    text: str
    approx_tokens: int
    pinned: bool = False
    pin_reason: str = ""
    recent: bool = False
    droppable: bool = False  # deterministic pre-filter for judging
    duplicate_of: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def positions(self) -> list[int]:
        return self.indices


def _text_of(message: dict) -> str:
    content = message.get("content")
    parts = [content] if isinstance(content, str) else []
    extra = message.get("extra")
    if isinstance(extra, dict):
        if extra.get("actions"):
            parts.append(" ".join(str(a.get("command", "")) for a in extra["actions"]))
        raw = extra.get("raw_output")
        if isinstance(raw, str):
            parts.append(raw)
    return "\n".join(p for p in parts if p)


def prepare_items(messages: list[dict], *, chars_per_token: int = 4, recent_tokens: int = 12000,
                  scope_terms: list[str] | None = None, min_item_tokens: int = 60) -> list[Item]:
    """Group messages into items, pin consequences, mark recency, estimate tokens."""
    scope_terms = [t.lower() for t in (scope_terms or []) if t]
    items: list[Item] = []
    seen_texts: dict[str, str] = {}
    term_matches: list[tuple[str, str]] = []  # (scope term, item id) in encounter order
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role", "")
        indices = [index]
        # A turn = assistant message plus every immediately following tool result.
        if role == "assistant" and (message.get("tool_calls") or (message.get("extra") or {}).get("actions")):
            lookahead = index + 1
            while lookahead < len(messages) and messages[lookahead].get("role") == "tool":
                indices.append(lookahead)
                lookahead += 1
        text = "\n".join(_text_of(messages[i]) for i in indices)
        tool_text = "\n".join(str(messages[i].get("content") or "") for i in indices if messages[i].get("role") == "tool")
        kind = {"system": "system", "exit": "exit"}.get(role, "turn")
        if role == "user":
            kind = "task" if index == 1 else "notice"
            if (message.get("extra") or {}).get("interrupt_type"):
                kind = "notice"
        item = Item(
            id=f"item{index}",
            indices=indices,
            kind=kind,
            text=text,
            approx_tokens=max(1, len(text) // chars_per_token),
            meta={"role": role},
        )
        # pins: instructions, credentials, failing evidence, quarantine warnings
        if role == "system" or (role == "user" and index == 1):
            item.pinned, item.pin_reason = True, "instructions/task (never judged)"
        elif contains_credential_like(text):
            item.pinned, item.pin_reason = True, "credential-like content"
        else:
            for name, pattern in PIN_PATTERNS:
                if pattern.search(text):
                    item.pinned, item.pin_reason = True, name
                    break
        # "Anything the task references" is judged at the *file* level below: the
        # newest item that read/edited a referenced file is pinned, older ones are stale.
        if not item.pinned and scope_terms and tool_text:
            haystack = text.lower()
            for term in scope_terms:
                if term and term in haystack:
                    term_matches.append((term, item.id))
        # duplicate detection (older duplicates are cheap deterministic drops).
        # Compare the *tool result* text: the same command re-run yields the same
        # result, while the assistant's reasoning text differs and must not hide it.
        dedup_key = tool_text if len(tool_text) > 40 else text
        if not item.pinned and len(dedup_key) > 40:
            if dedup_key in seen_texts:
                item.droppable, item.duplicate_of = True, seen_texts[dedup_key]
            else:
                seen_texts[dedup_key] = item.id
        items.append(item)
        index = indices[-1] + 1

    # pin the newest snapshot per referenced term
    latest_by_term: dict[str, str] = {}
    for term, item_id in term_matches:
        latest_by_term[term] = item_id
    newest_ids = set(latest_by_term.values())
    for item in items:
        if item.id in newest_ids and not item.pinned:
            item.pinned, item.pin_reason = True, "current content of a task-referenced file"

    # recency exemption: most recent items up to recent_tokens are never touched
    budget = recent_tokens
    for item in reversed(items):
        item.recent = True
        budget -= item.approx_tokens
        if budget <= 0:
            break

    # judging candidates: not pinned, not recent, not too small — plus duplicates
    for item in items:
        if item.duplicate_of:
            item.droppable = True
        elif item.pinned or item.recent:
            item.droppable = False
        else:
            item.droppable = item.approx_tokens >= min_item_tokens
    return items


def estimate_tokens(messages: list[dict], chars_per_token: int = 4, items: list[Item] | None = None) -> int:
    if items is not None:
        return sum(i.approx_tokens for i in items)
    return max(1, sum(len(_text_of(m)) for m in messages) // chars_per_token)


def build_item_state(item: Item, *, task_excerpt: str, step: int, max_chars: int, chars_per_token: int = 4) -> str:
    """Compact per-item state for the judge (never the whole conversation)."""
    task = (task_excerpt or "").strip().replace("\n", " ")[:180]
    head = f"Harness task: {task}\nCompaction step: {step}\nItem kind: {item.kind}\nItem content:\n"
    room = max(80, max_chars - len(head))
    if len(item.text) <= room:
        body = item.text
    else:
        half = room // 2
        body = item.text[:half] + "\n...\n" + item.text[-half:]
    return (head + body)[: max_chars + 64]
