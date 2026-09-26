#!/usr/bin/env python3
"""
Judge calibration: fit temperatures on labelled fixture items (shadow mode).

Why it exists: Phase 2.6 requires judging in shadow mode on our own fixtures before
trusting thresholds, refitting temperature per laya's docs, and recording the
numbers for the calibration appendix. This script is that procedure, reproducible
with one command and no model-endpoint credentials (laya runs locally).

Usage:
    .venv/bin/python scripts/calibrate_judge.py                # fit + write config/calibration.json
    .venv/bin/python scripts/calibrate_judge.py --dry-run      # report numbers, do not write

Hackathon criteria served: Phase 2.6 (shadow mode + evidence-based thresholds),
rule 9 (documented, reproducible calibration).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.config import load_policy_config  # noqa: E402
from harness.guardrails.judgments import action_questions, compaction_questions, injection_question, scope_question  # noqa: E402
from harness.laya.calibration import DEFAULT_PATH, apply_temperature_noul, fit_temperature  # noqa: E402
from harness.laya.fallback import HeuristicJudge  # noqa: E402

QUESTION_SETS = {
    "action": action_questions,
    "injection": injection_question,
    "scope": scope_question,
    "compaction": compaction_questions,
}
ITEMS_PATH = REPO_ROOT / "tests" / "fixtures" / "judge_items.jsonl"


def load_items(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def extract_probabilities(item_set: str, judgement: dict) -> dict[str, float]:
    """Map a judgement to {bucket: probability} for fitting.

    For `compaction`, `essential` is fitted on the label as-is (1 = essential) and
    `drop` on the inverted label (1 = droppable), because the fixture labels encode
    "should be kept".
    """
    answers = judgement.get("answers") or {}
    if item_set == "action":
        probs = (answers.get("verdict") or {}).get("raw_probabilities") or {}
        return {"action_risk": float(probs.get("destructive", 0.0)) + float(probs.get("out_of_scope", 0.0))}
    if item_set == "injection":
        p = (answers.get("injection") or {}).get("raw_p_true")
        return {"injection": float(p)} if p is not None else {}
    if item_set == "scope":
        p = (answers.get("in_scope") or {}).get("raw_p_true")
        return {"in_scope": float(p)} if p is not None else {}
    if item_set == "compaction":
        out: dict[str, float] = {}
        p_ess = (answers.get("essential") or {}).get("raw_p_true")
        probs = (answers.get("verdict") or {}).get("raw_probabilities") or {}
        if p_ess is not None:
            out["essential"] = float(p_ess)
        if probs:
            out["drop"] = float(probs.get("drop", 0.0))
        return out
    return {}


INVERTED_BUCKETS = {"drop"}


def decide(probability: float, refuse: float, ask: float) -> str:
    if probability >= refuse:
        return "refuse"
    if probability >= ask:
        return "ask"
    return "act"


def main() -> int:
    parser = argparse.ArgumentParser(description="fit judge temperatures on fixture items")
    parser.add_argument("--items", default=str(ITEMS_PATH))
    parser.add_argument("--out", default=str(DEFAULT_PATH))
    parser.add_argument("--dry-run", action="store_true", help="report numbers without writing the calibration file")
    args = parser.parse_args()

    items = load_items(Path(args.items))
    policy_cfg = load_policy_config()
    judge_cfg = policy_cfg["guardrails"]["judge"]
    refuse_t = float(judge_cfg["refuse_threshold"])
    ask_t = float(judge_cfg["ask_threshold"])

    try:
        from harness.laya.adapter import LayaJudge

        judge = LayaJudge(config=judge_cfg)
        backend = "laya" if judge.available else "unavailable"
    except Exception as exc:
        judge, backend = None, f"unavailable ({type(exc).__name__})"
    if backend != "laya":
        print(f"[calibrate] laya unavailable ({backend}); fitting on the heuristic judge instead")
        judge = HeuristicJudge()

    by_set: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_set[item["set"]].append(item)

    bucket_values: dict[str, list[tuple[float, int]]] = defaultdict(list)

    for item_set, set_items in by_set.items():
        questions = QUESTION_SETS[item_set]()
        judgements = judge.judge_batch([i["state"] for i in set_items], questions)
        for item, judgement in zip(set_items, judgements):
            label = int(item["label"])
            for bucket, probability in extract_probabilities(item_set, judgement).items():
                bucket_label = 1 - label if bucket in INVERTED_BUCKETS else label
                bucket_values[bucket].append((probability, bucket_label))

    results: dict[str, dict] = {}
    decisions_before = {"act": 0, "ask": 0, "refuse": 0}
    decisions_after = {"act": 0, "ask": 0, "refuse": 0}
    for bucket, pairs in sorted(bucket_values.items()):
        probabilities = [p for p, _ in pairs]
        labels = [y for _, y in pairs]
        temperature, metrics = fit_temperature(probabilities, labels)
        results[bucket] = {"T": round(temperature, 4), "provenance": f"fixture fit on {Path(args.items).name}", **metrics}
        if bucket == "action_risk":
            for p in probabilities:
                decisions_before[decide(p, refuse_t, ask_t)] += 1
            for p in probabilities:
                decisions_after[decide(apply_temperature_noul(p, temperature), refuse_t, ask_t)] += 1
        print(
            f"[calibrate] {bucket:14s} n={len(labels):3d} rawECE={metrics['ece_before']:.3f} "
            f"-> calibrated ECE={metrics['ece_after']:.3f} (T={temperature:.3f}, acc@0.5={metrics['acc_before']:.2f})"
        )

    calibration = {
        "version": 1,
        "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "provenance": f"fixture fit on {Path(args.items).name} ({len(items)} labelled items), shadow mode, judge={backend}",
        "buckets": len(results),
        "thresholds_at_fit_time": {"refuse": refuse_t, "ask": ask_t},
        "decisions_at_thresholds": {"raw": decisions_before, "calibrated": decisions_after},
        "temperatures": results,
    }
    print(f"\n[calibrate] action-band decisions raw={decisions_before} calibrated={decisions_after}")
    if results:
        print(f"[calibrate] mean ECE {sum(r['ece_before'] for r in results.values())/len(results):.3f} -> "
              f"{sum(r['ece_after'] for r in results.values())/len(results):.3f}")
    if args.dry_run:
        print("[calibrate] dry-run: calibration file not written")
        return 0
    Path(args.out).write_text(json.dumps(calibration, indent=2) + "\n", encoding="utf-8")
    print(f"[calibrate] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
