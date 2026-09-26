"""
laya adapter — the local calibrated decision model, wrapped fail-soft.

Why it exists: the decision layer must never be able to break a run. This adapter
loads laya lazily, judges *batches* of compact states in one forward pass, applies
our fitted temperature per (question type, option count) bucket, and degrades to a
documented heuristic judge (or to no judge at all) on any failure.

Hard constraint respected: laya's state window is small (~320 tokens English,
~768 multilingual). Callers hand us *compact per-item states*, never conversation
context; this module additionally enforces a character budget and truncates.

Hackathon criteria served: Phase 1.1/2.2 (one local decision model powering both
layers), fallback-first pattern, rule 8 (laya is infrastructure, never the task
model).
"""

from __future__ import annotations

import concurrent.futures as futures
import time
from pathlib import Path

from harness.laya.calibration import (
    apply_temperature,
    apply_temperature_noul,
    load_calibration,
    temperature_for,
)

DEFAULT_CHECKPOINT = "convaiinnovations/laya"


class LayaJudge:
    """Fail-soft wrapper around a laya checkpoint."""

    source = "laya"

    def __init__(
        self,
        *,
        config: dict | None = None,
        telemetry=None,
        task_excerpt: str = "",
        checkpoint: str | None = None,
        calibration_path: Path | str | None = None,
    ):
        cfg = (config or {}).get("judge", config or {}) or {}
        self.telemetry = telemetry
        self.task_excerpt = task_excerpt
        self.checkpoint = checkpoint or cfg.get("model", DEFAULT_CHECKPOINT)
        self.max_len = int(cfg.get("max_len", 512))
        self.head_max_len = int(cfg.get("head_max_len", 192))
        self.load_timeout_s = float(cfg.get("load_timeout_s", 900))
        self.predict_timeout_s = float(cfg.get("predict_timeout_s", 30))
        self.max_state_chars = int(cfg.get("max_state_chars", 1100))
        self.calibration = load_calibration(calibration_path)
        self._agent = None
        self._load_error: str | None = None
        self._load_attempted = False
        self._executor = futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya")
        self.load_ms = 0
        self.calls = 0
        self.total_ms = 0

    # ------------------------------------------------------------------ load
    @property
    def available(self) -> bool:
        if self._agent is not None:
            return True
        if self._load_attempted:
            return False
        return self._ensure_loaded()

    def _ensure_loaded(self) -> bool:
        # Load at most once per judge instance: reloading the checkpoint per batch
        # was both slow and misleading in telemetry (one laya_loaded event per call).
        if self._agent is not None:
            return True
        if self._load_attempted:
            return False
        self._load_attempted = True
        return self._load()

    def _load(self) -> bool:
        started = time.time()
        try:
            import laya  # noqa: PLC0415 - optional dependency, imported lazily by design

            self._agent = self._with_timeout(lambda: laya.load(self.checkpoint), self.load_timeout_s, what="load")
        except Exception as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
            self._agent = None
            if self.telemetry is not None:
                self.telemetry.degrade("laya", f"checkpoint unavailable ({self._load_error}); using heuristics")
            return False
        self.load_ms = int((time.time() - started) * 1000)
        if self.telemetry is not None:
            self.telemetry.emit("laya_loaded", checkpoint=self.checkpoint, load_ms=self.load_ms,
                                calibration=self.calibration.get("provenance", "unknown"))
        return True

    def _with_timeout(self, fn, timeout_s: float, what: str):
        future = self._executor.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except futures.TimeoutError:
            raise TimeoutError(f"laya {what} exceeded {timeout_s:.0f}s") from None

    # ----------------------------------------------------------------- judge
    def judge_batch(self, states: list[str], questions: dict) -> list[dict]:
        """One forward pass for all (state, question-set) pairs. Never raises."""
        if not self._ensure_loaded():
            return [self._unavailable(self._load_error or "laya not loaded") for _ in states]
        trimmed = [s[: self.max_state_chars] for s in states]
        started = time.time()
        try:
            results = self._with_timeout(
                lambda: self._agent.predict_batch(trimmed, questions, max_len=self.max_len, head_max_len=self.head_max_len),
                self.predict_timeout_s,
                what="predict",
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if self.telemetry is not None:
                self.telemetry.degrade("laya", f"predict failed ({reason}); using heuristics")
            return [self._unavailable(reason) for _ in states]
        elapsed_ms = int((time.time() - started) * 1000)
        self.calls += 1
        self.total_ms += elapsed_ms
        out = []
        for result in results:
            out.append(self._normalize(result, questions, elapsed_ms / max(len(results), 1)))
        return out

    # ------------------------------------------------------------- internals
    def _normalize(self, result: dict, questions: dict, latency_ms: float) -> dict:
        answers_raw = (result or {}).get("answers") or {}
        answers: dict[str, dict] = {}
        applied: dict[str, dict] = {}
        for qid, raw in answers_raw.items():
            qtype = raw.get("type", questions.get(qid, {}).get("type", ""))
            raw_probs = raw.get("probabilities")
            if qtype == "choice":
                keys = list(raw_probs.keys()) if raw_probs else list(questions.get(qid, {}).get("criteria", {}).keys())
                probs = [float(raw_probs[k]) for k in keys] if raw_probs else []
                temperature, provenance = temperature_for(self.calibration, "choice", len(keys))
                calib_probs = apply_temperature(probs, temperature) if probs else []
                applied[qid] = {"temperature": round(temperature, 4), "provenance": provenance, "n_options": len(keys)}
                answers[qid] = {
                    "type": "choice",
                    "labels": keys,
                    "label": raw.get("choice"),
                    "calibrated_label": keys[calib_probs.index(max(calib_probs))] if calib_probs else raw.get("choice"),
                    "probabilities": {k: round(p, 4) for k, p in zip(keys, calib_probs)} if calib_probs else {},
                    "raw_probabilities": {k: round(float(raw_probs[k]), 4) for k in keys} if raw_probs else {},
                    "confidence": raw.get("confidence"),
                    "answer_confidence": raw.get("answer_confidence"),
                }
            elif qtype == "score":
                probs = [float(v) for _, v in sorted((raw.get("probabilities") or {}).items(), key=lambda kv: int(kv[0]))]
                temperature, provenance = temperature_for(self.calibration, "score", len(probs))
                calib_probs = apply_temperature(probs, temperature) if probs else []
                applied[qid] = {"temperature": round(temperature, 4), "provenance": provenance, "n_options": len(probs)}
                score = sum(i * p for i, p in enumerate(calib_probs)) if calib_probs else raw.get("score")
                answers[qid] = {
                    "type": "score",
                    "score": round(float(score), 4),
                    "raw_score": raw.get("score"),
                    "probabilities": {str(i): round(p, 4) for i, p in enumerate(calib_probs)},
                    "answer_confidence": raw.get("answer_confidence"),
                }
            else:  # noul
                p_true = float(raw.get("noul", 0.0))
                temperature, provenance = temperature_for(self.calibration, "noul", 2)
                calibrated = apply_temperature_noul(p_true, temperature)
                applied[qid] = {"temperature": round(temperature, 4), "provenance": provenance, "n_options": 2}
                answers[qid] = {
                    "type": "noul",
                    "p_true": round(calibrated, 4),
                    "raw_p_true": round(p_true, 4),
                    "label": "true" if calibrated >= 0.5 else "false",
                    "answer_confidence": raw.get("answer_confidence"),
                }
        return {
            "answers": answers,
            "source": "laya",
            "latency_ms": round(latency_ms, 1),
            "calibration": applied,
            "model": (result or {}).get("model", "laya"),
            "usage": (result or {}).get("usage", {}),
        }

    def _unavailable(self, reason: str) -> dict:
        return {"answers": {}, "source": "unavailable", "latency_ms": 0.0, "error": reason}

    def status(self) -> dict:
        return {
            "checkpoint": self.checkpoint,
            "available": self._agent is not None,
            "load_error": self._load_error,
            "load_ms": self.load_ms,
            "calls": self.calls,
            "mean_ms": round(self.total_ms / self.calls, 1) if self.calls else None,
            "calibration": self.calibration.get("provenance", "unknown"),
        }


def risk_from_action_answers(answers: dict) -> tuple[str, float]:
    """Derive (label, risk probability) from an action-question answer set."""
    verdict = answers.get("verdict") or {}
    probs = verdict.get("probabilities") or {}
    if probs:
        risk = float(probs.get("destructive", 0.0)) + float(probs.get("out_of_scope", 0.0))
        label = max(probs, key=probs.get)
        return label, min(max(risk, 0.0), 1.0)
    noul = answers.get("injection") or answers.get("in_scope") or {}
    if "p_true" in noul:
        return ("true" if noul["p_true"] >= 0.5 else "false"), float(noul["p_true"])
    return "unknown", 0.0
