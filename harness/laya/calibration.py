"""
Calibration — temperature scaling for the local judge, measured on our own fixtures.

Why it exists: laya's own documentation says both checkpoints ship over-confident
("Refitting one temperature per (question type, option count) on held-out data moves
mean ECE 0.466 -> 0.081 ..."). Our thresholds (refuse 0.85, ask 0.55) are only
meaningful if the probabilities they read are calibrated, so this module fits one
temperature per bucket on labelled fixture items collected in shadow mode, clamps
it to laya's documented [0.5, 5.0] range, and records ECE before/after in the
appendix.

Hackathon criteria served: Phase 2.6 (shadow mode, thresholds from evidence,
temperature refit, appendix numbers), rule 9 (documented, reproducible numbers).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

T_MIN, T_MAX = 0.5, 5.0
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "calibration.json"


def load_calibration(path: Path | str | None = None) -> dict:
    path = Path(path or DEFAULT_PATH)
    if not path.exists():
        return {"version": 1, "provenance": "missing file; neutral T=1.0", "temperatures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "provenance": f"unreadable file {path}; neutral T=1.0", "temperatures": {}}
    data.setdefault("temperatures", {})
    return data


def bucket_key(question_type: str, n_options: int) -> str:
    return f"{question_type}:{n_options}"


def temperature_for(calibration: dict, question_type: str, n_options: int) -> tuple[float, str]:
    """Return (temperature, provenance) for a bucket, clamped to laya's range."""
    entry = (calibration.get("temperatures") or {}).get(bucket_key(question_type, n_options))
    if isinstance(entry, dict):
        t_raw = entry.get("T")
    else:
        t_raw = entry
    try:
        t = float(t_raw)
    except (TypeError, ValueError):
        return 1.0, "unfitted (neutral T=1.0)"
    if not math.isfinite(t):
        return 1.0, "non-finite fitted value; neutral T=1.0"
    return min(max(t, T_MIN), T_MAX), (entry.get("provenance", "fitted") if isinstance(entry, dict) else "fitted")


def _softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    exps = [math.exp(z - m) for z in logits]
    total = sum(exps)
    return [e / total for e in exps]


def apply_temperature(probabilities: list[float], temperature: float) -> list[float]:
    """Temperature-scale a probability vector: p -> softmax(log(p)/T)."""
    if not probabilities:
        return []
    if abs(temperature - 1.0) < 1e-9:
        return list(probabilities)
    logits = [math.log(max(p, 1e-12)) / max(temperature, 1e-9) for p in probabilities]
    return _softmax(logits)


def apply_temperature_noul(p_true: float, temperature: float) -> float:
    if abs(temperature - 1.0) < 1e-9:
        return p_true
    logit = math.log(max(min(p_true, 1 - 1e-12), 1e-12) / max(1 - min(max(p_true, 1e-12), 1 - 1e-12), 1e-12))
    scaled = logit / max(temperature, 1e-9)
    return 1.0 / (1.0 + math.exp(-scaled))


def calibrate_binary(calibration: dict | None, bucket: str, p: float) -> tuple[float, float]:
    """Apply our fitted temperature to a binary probability for *bucket*.

    Returns (calibrated_probability, temperature). Neutral T=1.0 when unfitted.
    """
    if not calibration:
        return p, 1.0
    temperature, _ = temperature_for(calibration, bucket, 2)
    return apply_temperature_noul(float(p), temperature), temperature


def ece(probabilities: list[float], labels: list[int], n_bins: int = 10) -> float:
    """Expected calibration error for binary probabilities."""
    if not probabilities:
        return float("nan")
    total = len(probabilities)
    error = 0.0
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        idx = [i for i, p in enumerate(probabilities) if (lo < p <= hi) or (b == 0 and p == 0.0)]
        if not idx:
            continue
        mean_p = sum(probabilities[i] for i in idx) / len(idx)
        mean_y = sum(labels[i] for i in idx) / len(idx)
        error += (len(idx) / total) * abs(mean_p - mean_y)
    return error


def nll(probabilities: list[float], labels: list[int]) -> float:
    eps = 1e-12
    return -sum(
        math.log(max(p if y else 1 - p, eps)) for p, y in zip(probabilities, labels)
    ) / max(len(probabilities), 1)


def fit_temperature(probabilities: list[float], labels: list[int], grid: int = 200) -> tuple[float, dict]:
    """Grid-search T in [T_MIN, T_MAX] minimising NLL; returns (T, metrics)."""
    best_t, best_nll = 1.0, float("inf")
    for i in range(grid + 1):
        t = T_MIN + (T_MAX - T_MIN) * i / grid
        scaled = [apply_temperature_noul(p, t) for p in probabilities]
        value = nll(scaled, labels)
        if value < best_nll:
            best_t, best_nll = t, value
    scaled = [apply_temperature_noul(p, best_t) for p in probabilities]
    return best_t, {
        "n": len(probabilities),
        "ece_before": round(ece(probabilities, labels), 4),
        "ece_after": round(ece(scaled, labels), 4),
        "nll_before": round(nll(probabilities, labels), 4),
        "nll_after": round(best_nll, 4),
        "acc_before": round(sum(1 for p, y in zip(probabilities, labels) if (p >= 0.5) == bool(y)) / max(len(labels), 1), 4),
    }


def refit(items: list[dict], path: Path | str | None = None, provenance: str = "") -> dict:
    """Fit per-bucket temperatures from shadow-mode items and persist the file.

    Each item: {"bucket": "noul:2", "p_true": 0.87, "label": 1}
    """
    from collections import defaultdict

    buckets: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        buckets[item["bucket"]].append(item)
    temperatures: dict[str, dict] = {}
    for bucket, bucket_items in sorted(buckets.items()):
        probs = [float(i["p_true"]) for i in bucket_items]
        labels = [int(i["label"]) for i in bucket_items]
        t, metrics = fit_temperature(probs, labels)
        temperatures[bucket] = {"T": round(t, 4), "provenance": provenance or "fixture fit", **metrics}
    calibration = {
        "version": 1,
        "fitted_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "provenance": provenance or "fixture fit",
        "buckets": len(temperatures),
        "temperatures": temperatures,
    }
    if path:
        Path(path).write_text(json.dumps(calibration, indent=2) + "\n", encoding="utf-8")
    return calibration


def summarize(calibration: dict) -> str:
    lines = [f"calibration provenance: {calibration.get('provenance', 'unknown')}"]
    for bucket, entry in sorted((calibration.get("temperatures") or {}).items()):
        if isinstance(entry, dict):
            lines.append(
                f"  {bucket}: T={entry.get('T')} n={entry.get('n')} "
                f"ECE {entry.get('ece_before')} -> {entry.get('ece_after')}"
            )
    return "\n".join(lines)
