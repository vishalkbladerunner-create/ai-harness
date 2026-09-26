"""
Compactor — layer 2 (semantic selection) + layer 3 (deterministic fitting).

Why it exists: Phase 2 replaces upstream's context handling with a decision-logged
compactor: per-item keep/drop/shorten verdicts from the local judge against compact
states, batched into one forward pass, followed by token maths done *in code*.
Conservative by construction: pins always win, borderline items are kept, the most
recent tokens are exempt, and call/result pairs are never severed (items are
grouped in layer 1).

The canonical timeline (`agent.messages`) is never modified — `select()` returns
the *view* the model sees for one call. Shadow mode judges and logs but prunes
nothing, so thresholds can be validated on fixture runs before going live.

Hackathon criteria served: Phase 2.1-2.6 (pipeline, vocabulary, conservatism,
fallback, audit, shadow mode).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from harness.compaction.items import Item, build_item_state, estimate_tokens, prepare_items
from harness.guardrails.judgments import compaction_questions
from harness.laya.calibration import calibrate_binary


@dataclass
class Verdict:
    item_id: str
    kind: str
    verdict: str  # keep | drop | shorten
    p_drop: float | None = None
    p_essential: float | None = None
    staleness: float | None = None
    source: str = "deterministic"
    reason: str = ""
    tokens: int = 0


def _shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n[... compacted: {omitted} characters omitted by guarded-mini ...]\n{text[-tail:]}"


class Compactor:
    def __init__(self, config: dict, *, judge=None, telemetry=None, task_excerpt: str = "", scope_terms: list[str] | None = None):
        cfg = (config or {}).get("compaction", config or {}) or {}
        self.mode = cfg.get("mode", "shadow")
        self.target_tokens = int(cfg.get("target_tokens", 60000))
        self.recent_tokens = int(cfg.get("recent_tokens", 12000))
        self.max_judge_items = int(cfg.get("max_judge_items", 40))
        self.min_item_tokens = int(cfg.get("min_item_tokens", 60))
        self.drop_threshold = float(cfg.get("drop_threshold", 0.60))
        self.shorten_threshold = float(cfg.get("shorten_threshold", 0.50))
        self.essential_threshold = float(cfg.get("essential_threshold", 0.50))
        self.staleness_min = float(cfg.get("staleness_min", 1.5))
        self.shorten_chars = int(cfg.get("shorten_chars", 1200))
        self.chars_per_token = int(cfg.get("chars_per_token", 4))
        self.trace_marker = bool(cfg.get("trace_marker", True))
        self.judge = judge
        self.telemetry = telemetry
        self.task_excerpt = task_excerpt
        self.scope_terms = scope_terms or []
        self.steps = 0
        self.stats = {
            "runs": 0,
            "items_judged": 0,
            "dropped": 0,
            "shortened": 0,
            "rescued": 0,
            "kept": 0,
            "tokens_before": 0,
            "tokens_after": 0,
            "tokens_saved_estimate": 0,
            "messages_before": 0,
            "messages_after": 0,
        }

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "live")

    # ------------------------------------------------------------------ entry
    def select(self, messages: list[dict]) -> list[dict]:
        """Return the message view for the next model call (may be the same list)."""
        if not self.enabled:
            return messages
        self.steps += 1
        items = prepare_items(
            messages,
            chars_per_token=self.chars_per_token,
            recent_tokens=self.recent_tokens,
            scope_terms=self.scope_terms,
            min_item_tokens=self.min_item_tokens,
        )
        total = estimate_tokens(messages, self.chars_per_token, items)
        if total <= self.target_tokens:
            return messages

        candidates = [i for i in items if (i.droppable or i.duplicate_of) and not i.pinned and not i.recent]
        candidates.sort(key=lambda i: (i.duplicate_of is None, -i.approx_tokens))  # duplicates first, then biggest
        candidates = candidates[: self.max_judge_items]

        verdicts, judge_source, judge_error = self._judge_items(candidates)
        verdict_map = {v.item_id: v for v in verdicts}

        # deterministic fitting
        dropped: set[str] = set()
        shortened: dict[str, str] = {}
        for item in items:
            verdict = verdict_map.get(item.id)
            if verdict is None:
                continue
            if verdict.verdict == "drop":
                dropped.add(item.id)
            elif verdict.verdict == "shorten":
                shortened[item.id] = _shorten_text(item.text, self.shorten_chars)

        visible = self._build_view(items, messages, dropped, shortened)
        tokens_after = estimate_tokens(visible, self.chars_per_token)
        # layer-3 fitting: if still over target, drop remaining droppable items (largest first)
        extra_drops: list[str] = []
        if tokens_after > self.target_tokens:
            remaining = [i for i in items if i.droppable and not i.pinned and not i.recent and i.id not in dropped]
            remaining.sort(key=lambda i: -i.approx_tokens)
            for item in remaining:
                if tokens_after <= self.target_tokens:
                    break
                dropped.add(item.id)
                extra_drops.append(item.id)
                tokens_after -= item.approx_tokens
            visible = self._build_view(items, messages, dropped, shortened)
            tokens_after = estimate_tokens(visible, self.chars_per_token)

        shadow = self.mode == "shadow"
        result = messages if shadow or (not dropped and not shortened) else visible

        self._record(items, verdict_map, dropped, shortened, extra_drops, total, tokens_after, shadow, judge_source, judge_error)
        return result

    # ---------------------------------------------------------------- judging
    def _judge_items(self, candidates: list[Item]) -> tuple[list[Verdict], str, str]:
        verdicts: list[Verdict] = []
        for item in candidates:
            if item.duplicate_of:
                verdicts.append(
                    Verdict(item.id, item.kind, "drop", source="deterministic",
                            reason=f"duplicate of {item.duplicate_of}", tokens=item.approx_tokens)
                )
        to_judge = [i for i in candidates if not i.duplicate_of]
        if not to_judge:
            return verdicts, "deterministic", ""

        judge_available = self.judge is not None and getattr(self.judge, "available", False)
        if not judge_available:
            if self.telemetry is not None:
                self.telemetry.degrade("compaction_judge", "laya unavailable; size/recency heuristics only")
            for item in to_judge:
                verdicts.append(
                    Verdict(item.id, item.kind, "keep", source="heuristic",
                            reason="judge unavailable; conservative keep", tokens=item.approx_tokens)
                )
            return verdicts, "heuristic", "judge unavailable"

        states = [build_item_state(i, task_excerpt=self.task_excerpt, step=self.steps, max_chars=int(getattr(self.judge, "max_state_chars", 1100)), chars_per_token=self.chars_per_token) for i in to_judge]
        started = time.time()
        try:
            results = self.judge.judge_batch(states, compaction_questions())
        except Exception as exc:  # never let the judge break the run
            if self.telemetry is not None:
                self.telemetry.degrade("compaction_judge", f"{type(exc).__name__}: {exc}")
            return verdicts + [Verdict(i.id, i.kind, "keep", source="heuristic", reason="judge raised; conservative keep", tokens=i.approx_tokens) for i in to_judge], "heuristic", str(exc)
        latency_ms = (time.time() - started) * 1000

        for item, result in zip(to_judge, results):
            if not result or result.get("source") == "unavailable":
                verdicts.append(Verdict(item.id, item.kind, "keep", source="heuristic", reason="judge unavailable per item", tokens=item.approx_tokens))
                continue
            answers = result.get("answers") or {}
            verdict_answer = answers.get("verdict") or {}
            p_drop_raw = float((verdict_answer.get("raw_probabilities") or {}).get("drop", 0.0))
            p_drop, t_drop = calibrate_binary(getattr(self.judge, "calibration", None), "drop", p_drop_raw)
            p_essential_raw = float((answers.get("essential") or {}).get("raw_p_true", 0.5))
            p_essential, t_ess = calibrate_binary(getattr(self.judge, "calibration", None), "essential", p_essential_raw)
            staleness = float((answers.get("staleness") or {}).get("score", 1.0))
            verdict = "keep"
            reason = ""
            if p_drop >= self.drop_threshold and p_essential < self.essential_threshold:
                if staleness < self.staleness_min:
                    verdict, reason = "keep", f"rescued: staleness {staleness:.2f} below {self.staleness_min}"
                else:
                    verdict, reason = "drop", f"P(drop)={p_drop:.2f} P(essential)={p_essential:.2f} staleness={staleness:.2f}"
            elif float((verdict_answer.get("probabilities") or {}).get("shorten", 0.0)) >= self.shorten_threshold and item.approx_tokens > self.min_item_tokens:
                verdict, reason = "shorten", f"P(shorten) high; {item.approx_tokens} tokens -> {self.shorten_chars} chars"
            else:
                reason = f"borderline: P(drop)={p_drop:.2f} P(essential)={p_essential:.2f} -> keep (conservative)"
            verdicts.append(
                Verdict(item.id, item.kind, verdict, p_drop=p_drop, p_essential=p_essential, staleness=staleness,
                        source=result.get("source", "laya"), reason=reason, tokens=item.approx_tokens)
            )
        # report the *active* judge (laya or the heuristic fallback), not a guess
        return verdicts, getattr(self.judge, "source", "laya"), ""

    # ------------------------------------------------------------------ fitting
    def _build_view(self, items: list[Item], messages: list[dict], dropped: set[str], shortened: dict[str, str]) -> list[dict]:
        view: list[dict] = []
        marker_added = False
        for item in items:
            if item.id in dropped:
                if self.trace_marker and not marker_added:
                    view.append(
                        {
                            "role": "user",
                            "content": (
                                "<compaction>Earlier conversation items were omitted to save context "
                                "(stale or superseded). The full history remains in the run log; re-run a "
                                "command if you need its result again.</compaction>"
                            ),
                        }
                    )
                    marker_added = True
                continue
            for position in item.indices:
                message = dict(messages[position])
                if item.id in shortened and message.get("role") == "tool" and isinstance(message.get("content"), str):
                    message["content"] = _shorten_text(message["content"], self.shorten_chars)
                view.append(message)
        return view

    # ------------------------------------------------------------------- audit
    def _record(self, items, verdict_map, dropped, shortened, extra_drops, tokens_before, tokens_after, shadow, judge_source, judge_error) -> None:
        self.stats["runs"] += 1
        self.stats["items_judged"] += len(verdict_map)
        self.stats["dropped"] += len(dropped)
        self.stats["shortened"] += len(shortened)
        for verdict in verdict_map.values():
            if verdict.verdict == "keep" and verdict.reason.startswith("rescued"):
                self.stats["rescued"] += 1
            elif verdict.verdict == "keep":
                self.stats["kept"] += 1
        self.stats["tokens_before"] = tokens_before
        self.stats["tokens_after"] = tokens_after
        self.stats["tokens_saved_estimate"] += max(0, tokens_before - tokens_after)
        self.stats["messages_before"] = len(items)
        if self.telemetry is None:
            return
        verdict_payload = [
            {
                "item": v.item_id,
                "kind": v.kind,
                "verdict": v.verdict,
                "p_drop": v.p_drop,
                "p_essential": v.p_essential,
                "staleness": v.staleness,
                "source": v.source,
                "reason": v.reason[:160],
                "tokens": v.tokens,
            }
            for v in verdict_map.values()
        ]
        self.telemetry.emit(
            "compaction",
            step=self.steps,
            mode=self.mode,
            shadow=shadow,
            target_tokens=self.target_tokens,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=max(0, tokens_before - tokens_after),
            items_total=len(items),
            items_judged=len(verdict_map),
            dropped=sorted(dropped),
            extra_drops=extra_drops,
            shortened=sorted(shortened),
            pinned=[i.id for i in items if i.pinned],
            recent=[i.id for i in items if i.recent],
            judge_source=judge_source,
            judge_error=judge_error,
            verdicts=verdict_payload,
            latency_s=None,
        )

    def status(self) -> dict:
        return {"mode": self.mode, **self.stats}
