"""Calibration maths: temperature scaling reduces ECE, clamps, and persists."""

from __future__ import annotations

import json

from harness.laya.calibration import (
    T_MAX,
    T_MIN,
    apply_temperature_noul,
    bucket_key,
    ece,
    fit_temperature,
    load_calibration,
    refit,
    summarize,
    temperature_for,
)


def test_temperature_scaling_moves_probability_toward_half_for_overconfidence():
    assert apply_temperature_noul(0.99, 2.0) < 0.99
    assert apply_temperature_noul(0.99, 0.5) > 0.99
    assert apply_temperature_noul(0.7, 1.0) == 0.7


def test_fit_recovers_overconfident_probabilities():
    # 100 items, true rate 50%, model says 0.9 or 0.1 -> ECE should drop after fitting.
    probabilities = [0.9] * 50 + [0.1] * 50
    labels = [1] * 25 + [0] * 25 + [0] * 25 + [1] * 25
    before = ece(probabilities, labels)
    temperature, metrics = fit_temperature(probabilities, labels)
    assert T_MIN <= temperature <= T_MAX
    assert metrics["ece_after"] <= before
    assert metrics["n"] == 100


def test_load_missing_file_is_neutral():
    calibration = load_calibration("/nonexistent/path/calibration.json")
    assert "neutral" in calibration["provenance"]
    temperature, _ = temperature_for(calibration, "noul", 2)
    assert temperature == 1.0


def test_temperature_for_clamps_out_of_range(tmp_path):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"temperatures": {bucket_key("noul", 2): {"T": 99.0}}}), encoding="utf-8")
    temperature, _ = temperature_for(load_calibration(path), "noul", 2)
    assert temperature == T_MAX
    path.write_text(json.dumps({"temperatures": {bucket_key("noul", 2): {"T": "bogus"}}}), encoding="utf-8")
    assert temperature_for(load_calibration(path), "noul", 2)[0] == 1.0


def test_refit_writes_file_and_summary(tmp_path):
    items = [
        {"bucket": "noul:2", "p_true": 0.95 if i % 2 == 0 else 0.05, "label": 1 if i % 2 == 0 else 0}
        for i in range(60)
    ] + [
        {"bucket": "choice:3", "p_true": 0.8, "label": 1},
        {"bucket": "choice:3", "p_true": 0.2, "label": 0},
    ]
    path = tmp_path / "calibration.json"
    calibration = refit(items, path, provenance="unit test")
    assert path.exists()
    assert calibration["buckets"] == 2
    assert "unit-test" in summarize(calibration).replace(" ", "-")
