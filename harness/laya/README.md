# laya sidecar — install notes, fallback behaviour, calibration

This directory is our adapter around **laya** (Apache-2.0, `convaiinnovations/laya`), the local
calibrated decision model that powers the guardrail second opinion and Phase-2 context compaction.

## What laya is (and is not)

* It **is** a local, CPU-friendly (~421M-parameter) non-autoregressive classifier that answers *typed*
  questions (`choice`, `score`, `noul` = P(true)) in one forward pass with calibrated probabilities.
* It is **not** the task model. The DeepSeek/Qwen endpoint does the software-engineering work; laya
  only judges small, typed infrastructure questions (is this command risky? is this item still
  needed?). Rule 8 of the hackathon brief: infrastructure, like an embedding model or a linter.

## Install and cache (all project-local)

`make setup` performs both steps and **never fails the build** if they fail:

```text
.venv/bin/python scripts/setup_laya.py         # pip install "laya[serve]"  (non-fatal, 30-min timeout)
.venv/bin/python scripts/fetch_checkpoint.py   # snapshot_download into <repo>/.cache/huggingface (non-fatal)
```

Status files: `.cache/laya-setup.json`, `.cache/laya-checkpoint.json` (reason recorded when skipped).
`HF_HOME` is pinned to `<repo>/.cache/huggingface` before any import, so nothing lands in your home
directory and repeated setup runs reuse the cache.

## State-window constraint (hard rule)

laya's English checkpoint reads ~320 state tokens (~1100 characters); the multilingual one ~768.
**We never feed conversation context.** Every judgement is per-item against a compact state:

```text
Harness task: <task excerpt, <= 200 chars>
Working directory (workspace): <path>
Shell command to classify:
<command, <= 400 chars>
```

`harness/laya/adapter.py` additionally truncates to `max_state_chars` (config `policy.yaml`), and the
compaction layer builds its own per-item states the same way. Questions for many items are sent in
**one** `predict_batch` call — one forward pass per batch, never one call per item.

## Fallback behaviour (fail-soft by construction)

| situation | what happens |
|---|---|
| `import laya` fails | `LayaJudge.available` is False once, a `degradation` event is written, and `HeuristicJudge` takes over (conservative keyword rules, `source: heuristic`) |
| checkpoint load/predict times out or raises | same as above, with the exception text recorded |
| one question returns nothing usable | that item is treated as borderline (kept / allowed) and logged |
| judge disabled in `policy.yaml` | `build_judge` returns `None`; the deterministic layer decides alone and `degraded: true` is logged for grey-zone commands |

The report's "Degradation notes" section shows exactly which mode ran.

## Calibration

`scripts/calibrate_judge.py` fits one temperature per bucket on the labelled fixtures in
`tests/fixtures/judge_items.jsonl` (shadow mode: judge, compare, then adopt), clamps it to laya's
documented `[0.5, 5.0]`, and writes `harness/config/calibration.json` with ECE before/after and the
decision bands at our thresholds. `harness/laya/calibration.py` applies it at runtime.

Procedure (reproducible, no model-endpoint credentials needed):

```sh
.venv/bin/python scripts/calibrate_judge.py --dry-run   # numbers only
.venv/bin/python scripts/calibrate_judge.py             # writes config/calibration.json
```

## HTTP mode (optional, not on the evaluation path)

`laya[serve]` provides a FastAPI server (`python examples/server.py` in the laya repo). The adapters
run in-process by default; if you prefer a sidecar server, set `judge.mode: http` — not implemented
yet, and deliberately not required for the evaluation path.
