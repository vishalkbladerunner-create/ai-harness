# guarded-mini

**A minimal, guarded SWE harness with a local calibrated decision layer.**
Built on [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) (MIT, © the SWE-agent team),
pinned at **v2.4.6** (`a83fcae82d2a08f0ee0c688f9d137b3566c097f8`) and vendored byte-identical under
`vendor/mini-swe-agent/`.

The thesis: the frontier model (DeepSeek/Qwen) does the software engineering; a small local
classifier (**laya**, ~421M params, CPU-only) makes the *infrastructure* decisions a harness needs —
"is this command safe?", "is this context still needed?" — with calibrated probabilities, and every
decision is auditable in the run report.

```
        ┌──────────────────────────────────────────────────────────────────────┐
        │ make setup → .venv + pinned vendored core + harness (+ optional laya)│
        │ make run   → harness/entrypoint.py → harness/run.py                  │
        │ make test  → unit tests + mock-E2E on the fixture repo (no API)      │
        └──────────────────────────────────────────────────────────────────────┘
                                    │
                 ┌──────────────────┴──────────────────┐
                 │  HarnessAgent (extends upstream)    │
                 │   query() ─┬─ budget check          │
                 │            ├─ calibrated compaction │← laya (per-item)
                 │            └─ model call ───────────┼──► DeepSeek/Qwen endpoint
                 │   execute_actions()                 │    (provider discovery)
                 └──────────────┬──────────────────────┘
                                ▼
        ┌──────────────────────────────────────────────────────────────────────┐
        │ GuardedEnvironment.execute  (the one choke point)                    │
        │  sentinels → policy gate (deterministic + laya) → bash               │
        │  → verification block → injection scan → secret masking → log        │
        └──────────────────────────────┬───────────────────────────────────────┘
                                       │ every event
                    ┌──────────────────┴───────────────────┐
                    ▼                                      ▼
        ┌───────────────────────────┐        ┌─────────────────────────────────┐
        │ telemetry.jsonl           │        │ Live TUI (make run TUI=1)       │
        │  model_call (tokens,      │───────►│  context split │ project graph  │
        │  latency, context split)  │  tails │  agent stream  │ follow / quit  │
        │  command + guardrail …    │        └─────────────────────────────────┘
        └─────────────┬─────────────┘
                      ▼
        ┌──────────────────────────────────────────────────────────────────────┐
        │ REPORT.md (issue, context window, diff, tests, guardrails, scope,    │
        │            injection, compaction, degradations, budget, timeline)    │
        └──────────────────────────────────────────────────────────────────────┘
```

---

## Requirements

* **Python ≥ 3.10** (the pinned upstream core requires it; macOS system Python is 3.9). `make setup`
  looks for `python3.12`/`3.13`/`3.11`/`3.10` on PATH and in the usual install locations; if the
  machine only has an older Python, it downloads `uv` into `.cache/tools` and provisions a
  project-local CPython 3.12 automatically (`make setup BOOTSTRAP=python3.12` forces one).
* Network access during `make setup` (pip + the optional laya checkpoint); `make run` needs only the
  model endpoint.

---

## Quickstart — the evaluator path

```sh
git clone <this repo> && cd <repo>

export AI_API_KEY=...          # REQUIRED — the only variable the committee exports

make setup                     # venv + pinned core + harness + laya (laya is optional)
make run < issue.md            # or: make run ISSUE=issue.md
```

`AI_API_KEY` is read from the environment at runtime and **never written to disk** (the
report, telemetry and trajectory are all masked; the fixture E2E asserts it).

`MODEL_BASE_URL` and `MODEL_NAME` are **optional**. With only the key, the harness uses the
checked-in provider defaults — DeepSeek `deepseek-chat` → `deepseek-flash` → `deepseek-v4-pro`,
then Qwen `qwen-plus`/`qwen-max` (DashScope compatible endpoint) — and, if the key belongs to the
other provider, switches once on the 401 (skipping the other model names of the same provider) and
records a `provider_fallback` event in telemetry. This is provider *discovery*, not model
substitution: if the committee exports `MODEL_BASE_URL`/`MODEL_NAME` (their prescribed model),
those win verbatim and the switch is disabled. A valid key with no balance produces a clear
`insufficient balance/quota` error (no retries, no switch — the credential is never sent to
another provider).

The target repository is resolved, in order, from:

1. `WORKSPACE=…` on the make command line, `$HARNESS_WORKSPACE`, or `--workspace PATH`;
2. a `Workspace: /path` (or `Repo: /path`) line in the issue text;
3. an existing git checkout whose absolute path appears in the issue;
4. the current directory (the evaluator may run the harness inside the target repo);
5. the GitHub repository named by the issue (`https://github.com/owner/repo` or
   `Repo: owner/repo`), cloned into `.cache/workspaces/`;
6. otherwise the current directory, with a warning — `make run` never refuses to launch.

`make run` launches **our** entrypoint (`harness.entrypoint`), never the stock `mini` CLI.

### Committee workflow, step by step

The submission guidelines define: obtain the repository → `export AI_API_KEY` → `make setup` →
`make run` → the prescribed issue/test case is supplied to the running harness. Every step maps
1:1 to this repository:

| guideline step | here |
|---|---|
| obtain the repository | `git clone <repo> && cd <repo>` |
| configure the credential | `export AI_API_KEY="<PROVIDED_KEY>"` — the **only** variable required |
| `make setup` | creates `.venv`, installs the pinned vendored core + harness + test deps; laya/TUI deps are optional and non-fatal |
| `make run` | launches `harness.entrypoint` (unattended) |
| issue/test case supplied | **stdin is the contract**: `make run < issue.md` (pipe, paste, or TTY prompt). `make run ISSUE=issue.md` is the *same run* with the issue passed as a file path instead of stdin — a convenience, never a requirement |
| `make test` | unit tests + mock-endpoint E2E on the fixture repo (no credentials needed) |

The evaluator does not need `ISSUE=` (or any other flag) for the standard workflow; it exists so a
file path can be used when piping is inconvenient. `make run` accepts either, plus
`--issue FILE` and a positional path.

```sh
make run < issue.md                            # the committee path (stdin)
make run ISSUE=issue.md                        # equivalent, file path
make run WORKSPACE=/path/to/target-repo < issue.md
make run ARGS="--verify on --budget 10 --max-steps 40" < issue.md
```

Other targets: `make test` (unit tests + no-API E2E on the fixture repo, plus live E2E when
credentials are exported), `make smoke` (one trivial live task), `make clean`,
`make check-upstream` (proves the vendored core is byte-identical to upstream v2.4.6),
`make check-clean-env` (fresh-copy run of the whole evaluator workflow).

---

## Repository layout (submission checklist mapping)

| committee checklist | in this repository |
|---|---|
| `Makefile` (root) | `Makefile` — `setup` / `run` / `test` / `clean` (+ `smoke`, `check-upstream`, `check-clean-env`) |
| `README.md` | this file (quickstart, architecture, evaluation workflow) |
| source code | `harness/` (entrypoint, guardrails, compaction, telemetry, reporting, laya adapter) |
| configuration files | `harness/config/` (YAML policy/compaction/harness + prompts + calibration), `pyproject.toml` |
| dependency files | `constraints.txt` (tested pins), `pyproject.toml`, vendored `vendor/mini-swe-agent/pyproject.toml` |
| tests / evaluation procedure | `tests/unit/` (168 tests), `tests/fixture-repo/`, `scripts/e2e_fixture.py` (mock + live E2E) |
| documentation | `docs/ARCHITECTURE.md`, `NOTES-BUILD.md`, `reports/EXAMPLE/` (a captured run) |

---

## What is ours vs upstream (the honest split)

| path | owner | what it is |
|---|---|---|
| `vendor/mini-swe-agent/` | **upstream**, MIT, unmodified | the agent loop, bash tool, litellm model, local environment |
| `harness/` | **ours** | entrypoint, guardrails, compaction, telemetry, reporting, laya adapter, config |
| `harness/config/` | ours | all checked-in configuration + prompt overlay + calibration (no secrets) |
| `scripts/` | ours | setup, smoke, fixture E2E, calibration, compaction study, clean-env check |
| `tests/` | ours | unit tests + fixture repo used by `make test` |
| `docs/ARCHITECTURE.md` | ours | line-by-line walkthrough of the upstream loop and our hooks |
| `LICENSE` | upstream text, verbatim | MIT, © 2025 Kilian A. Lieret and Carlos E. Jimenez |
| `harness-lab/` | *not in this repo* | reference-only checkouts from recon, deliberately excluded |

We do not patch upstream. The one interaction is at documented seams: a subclassed agent
(`HarnessAgent.query`), a subclassed environment (`GuardedEnvironment.execute`), a subclassed model
(`HarnessModel.query`), and prompt templates *extended* from the vendored `mini.yaml`.

---

## Guardrails (Phase 1)

Every command passes `GuardedEnvironment.execute → ActionPolicy.check` before bash sees it.

1. **Deterministic rules first** (authoritative), configured in `harness/config/policy.yaml`:
   privilege escalation, filesystem destruction (`rm -rf /`, `mkfs`, `dd of=/dev/…`), credential
   access (`~/.ssh`, `.env`, `printenv *KEY*`), network egress (`curl`, `wget`, `git clone/fetch/push`,
   remote package installs), git history/staging (`git add/commit/rebase/…`), writes outside the
   workspace (real write-target extraction, not path-looking text), and unsafely backgrounded work.
2. **Writes statically confined to the workspace are allowed without judging** — that is the agent's
   normal work, and scope is checked separately on the final diff. Dynamic evaluation (`python -c`,
   `eval`, `base64 -d`, `xargs sh`) never counts as confined; when such a payload contains a
   write/delete/network primitive (`rmtree`, `os.remove`, `subprocess`, `requests`, `rm -rf`, …) it is
   refused deterministically instead of being delegated to the judge.
3. **The grey zone goes to laya** — a typed `choice` question (`safe` / `destructive` / `out_of_scope`)
   plus a `score` severity, one batched forward pass, with our fitted temperature applied
   (`harness/config/calibration.json`). Bands: act < 0.70 ≤ ask < 0.90 ≤ refuse.
4. **Unattended `ask` resolves to refuse-with-guidance**: the model receives an explanation, the
   calibrated probability, and a suggested safe alternative, then continues — the deny path is
   graceful, never fatal. (`guardrails.unattended: true`; `mode: shadow` judges and logs without
   blocking.)
5. **Scope guard**: end-of-run diff classification (issue terms → tests → project config → laya).
   Deterministic out-of-scope changes are flagged, reported, and **rolled back** in git workspaces;
   judge-only flags are reported but never reverted, and if no change is deterministically in scope
   we roll nothing back (a wrong rollback is worse than a reported oddity). When a deterministic
   in-scope change anchors the reading of the task, judge-flagged out-of-scope files are additionally
   **excluded from the submitted patch** while staying in the workspace (data is never lost).
6. **Injection defence**: repository content is data, never instructions. Deterministic patterns plus
   a laya `noul` probe flag instruction-shaped text; flagged observations carry a `<quarantine>`
   warning, and the compactor pins flagged items so a warning cannot be compacted away.
7. **Secret hygiene**: credential-like env values are *blanked* in the agent's child environment
   (omission is not enough — upstream merges `os.environ`), every log/report line passes through a
   masking choke point, the model configuration is redacted by field name before it is serialized
   into `trajectory.json`, and git staging/commits are refused (so nothing can be committed by the agent).
8. **Resource guardrails**: hard step / model-call / token / wall-clock budgets with a graceful
   `LimitsExceeded` exit; upstream process-group timeouts; an end-of-run sweep that kills leftover
   workspace-scoped background processes; retry cap 3 with exponential backoff.

## Calibrated compaction (Phase 2)

Strictly three layers: **deterministic preparation → semantic selection by laya → deterministic
fitting**, with the token maths done in code, never by the judge.

* Items are causal units (assistant call + its tool results); dropping a result also drops its call.
* Pins (never judged): system task instructions, credential-like content, failing-test evidence,
  quarantine warnings, and the *newest* snapshot of every file the issue references.
* The most recent ~12k tokens are exempt; duplicate tool results are dropped deterministically.
* Each remaining item is judged in **one batched forward pass** against a compact state
  (task + step + bounded item text ≤ ~1100 characters), never the conversation.
* Verdicts: `keep` / `drop` / `shorten`, gated by `P(drop) ≥ 0.60` **and** `P(essential) < 0.50`
  (both from the calibrated buckets), with a staleness rescue (`staleness < 1.5 → keep`) and a
  conservative default of *keep*.
* `mode: shadow` judges and logs without pruning; `mode: live` prunes; the canonical trajectory is
  never modified either way. A one-line `<compaction>` marker tells the model that earlier items were
  omitted, so it re-runs a command instead of guessing.
* Judge missing/failing → heuristics (`size + recency + consequence`), a `degradation` event, and the
  run continues.

## Self-verification and harness commands (Phase 3)

* After any command that changed the workspace, the detected test command runs
  (`pytest` / `npm test` / `make test`), and the result is appended to the observation as a
  `<verification status="PASS|FAIL">` block — that block *is* the feedback loop. Commands that are
  themselves the test command are not duplicated; their output is recorded as evidence.
* `harness_submit_patch` — produces the final scoped diff, records verification, and ends the run
  (implemented as a sentinel command: bash never sees it).
* `harness_load_issue` — re-prints the issue and its parsed metadata.
* A bounded repo map (tree + symbols) is injected at session start.

## Telemetry and the report (Phase 4)

`reports/<run-id>/` contains `REPORT.md`, `telemetry.jsonl`, `trajectory.json`, `patch.diff`,
`status.json`; `reports/LATEST` points at the newest run. The JSONL logs every model call (latency,
tokens, cost, context split), every command with its guardrail decision (outcome, rule, probability,
temperature), every compaction verdict, every verification run, degradations, and the budget.
`REPORT.md` is the evaluation artefact: issue, summary, context window, reproducibility (pinned
temperature/seed), files changed with the diff, verification evidence, guardrail log, scope check,
injection scans, compaction audit, degradation notes, budget, timeline. The CLI and report header
are the team wordmark (Neuromancer), rendered from `harness/theme.py`.

## Live TUI — a view over the telemetry stream (optional)

The evaluation path is headless; the TUI is a **viewer** over the same run (it launches the normal
entrypoint as a child process and tails its JSONL), so the visual path and the evaluation path
cannot drift apart. The screen is three panels plus a status line:

```
┌─ guarded-mini ─────────────────────────────────────────────── run 20260927-… ── ⬡ Graph ─┐
│ openai/deepseek-chat · step 7 · 12.4k tokens · 38s · budget 4m12s/20m                     │
├──────────────────────────────────┬────────────────────────────────────────────────────────┤
│ CONTEXT SPLIT (per model call)   │ PROJECT GRAPH (git, refreshed every ~2 s)              │
│   system   ████        1.2k      │   fixture-repo/                                        │
│   tools    █             0.3k    │   ├── buggy.py            ● modified                   │
│   messages █████████   6.4k      │   ├── test_buggy.py       ○ touched by the run         │
│   free     ███████████ 54.6k     │   └── README.md                                        │
│   compaction saved 3.1k (4×)     │   (changed/untracked files highlighted)                │
├──────────────────────────────────┴────────────────────────────────────────────────────────┤
│ AGENT STREAM                                                                              │
│ 14:02:11 model_call  3.1k tok  0.8s  context 34%                                          │
│ 14:02:12 command     allow     in_workspace_write   sed -i 's/…/…/' buggy.py              │
│ 14:02:12 verification PASS      python -m pytest -q                                       │
│ 14:02:15 compaction  keep 3 / shorten 1 / drop 0   (laya, P(drop)=0.42)                   │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

* **Context split** (top-left) — what the next request is made of: `system` prompt, `tools`
  (the harness command surface), `messages`, and `free` window space, recomputed after every model
  call, plus the cumulative tokens pruned by compaction. The same numbers appear in the report's
  **Context window** section. Counting is tiktoken `cl100k_base` — an approximation for
  DeepSeek/Qwen tokenizers, labelled as such — and the window size comes from checked-in config
  (`model.context_limit`, with per-provider overrides), never hard-coded.
* **Project graph** (top-right) — the ground truth of what the run touched: a directory tree of the
  target workspace rebuilt from `git status`/`ls` every couple of seconds, with modified/untracked
  files highlighted (●) and files the agent read or edited marked (○). Toggle with the ⬡ button or
  `g`; it is deliberately independent of telemetry (a missing event cannot hide a file change).
* **Agent stream** (bottom) — every telemetry event as it is written: model calls with latency,
  tokens and context utilisation; commands with their guardrail decision, rule and calibrated
  probability; verification PASS/FAIL; compaction verdicts; degradations and budget warnings.

```sh
make run TUI=1 < issue.md                    # live, same headless run underneath
make run TUI=1 ARGS="--dry-run" < issue.md   # TUI demo with no credentials
make replay RUN=reports/LATEST               # open a finished run read-only (no model calls)
```

Keys: `g` graph, `f` follow, `q` quit. The TUI needs `textual` + `tiktoken` (installed by
`make setup`, non-fatally); without them `make run TUI=1` explains how to install them and the
headless path is unaffected.

## Model configuration and reproducibility

* Credentials are read **only** from the environment, at runtime, by `harness/config.read_env()`;
  they are never written to disk. Only `AI_API_KEY` is required; `.env.example` documents all
  variables with empty values.
* Endpoint and model name come from `MODEL_BASE_URL`/`MODEL_NAME` when the evaluator exports them;
  otherwise the checked-in `model.provider_defaults` in `harness/config/harness.yaml` are used
  (DeepSeek then Qwen) with a one-shot provider switch on a 401/404, recorded in telemetry.
  Either way the route is OpenAI-compatible (`openai/<name>` internally).
* Sampling parameters are pinned in checked-in config (`temperature: 0.0`, `seed: 42`); if an endpoint
  rejects `seed`/`temperature`/`max_tokens`, the harness retries once without them and records the
  degradation in the report.
* Dependency versions verified for this build (laya, litellm, torch, transformers, pytest, …) are
  recorded in `constraints.txt`; `make setup` applies them and retries unpinned with a warning if a
  pin cannot be satisfied on the evaluator's platform — setup never fails because of a constraint.
* **laya is infrastructure, not the task model** (hackathon rule 8): it judges small typed
  infrastructure questions with calibrated probabilities, exactly like an embedding model or a
  linter; the DeepSeek/Qwen endpoint performs all software-engineering work. If laya is absent the
  harness runs on documented heuristics.

## Calibration appendix (real numbers, produced by this build)

`scripts/calibrate_judge.py` fits one temperature per bucket on 64 labelled shadow-mode items
(`tests/fixtures/judge_items.jsonl`), clamped to laya's documented `[0.5, 5.0]`:

| bucket | n | ECE before | ECE after | fitted T | accuracy @0.5 |
|---|---|---|---|---|---|
| `action_risk` (guardrail risk = P(destructive)+P(out_of_scope)) | 28 | 0.209 | 0.200 | 0.522 | 0.68 |
| `drop` (compaction droppable) | 15 | 0.305 | 0.220 | 0.500 (clamp) | 0.80 |
| `essential` (compaction keep) | 15 | 0.417 | 0.312 | 5.000 (clamp) | 0.47 |
| `in_scope` (scope guard) | 9 | 0.371 | 0.216 | 4.933 | 0.33 |
| `injection` (prompt-injection probe) | 12 | 0.233 | 0.203 | 0.725 | 0.75 |

Honest reading: the base `laya` checkpoint is **weak zero-shot on these domain questions** (its own
docs say the base checkpoint sits below the majority-class baseline on typed-decisions). The fitted
temperatures make that explicit — `essential` and `in_scope` are flattened toward 0.5, which is why
the compaction policy keeps everything borderline and why scope rollback always requires a
deterministic anchor. The judge earns its place where the evidence is better (`drop`, `injection`,
`action_risk`), and the deterministic layer carries the load elsewhere. laya 0.3.20 also warns at load
that the shipped `laya` checkpoint contains an invalid temperature for a high-cardinality `choice`
bucket; we record the warning and fit our own scaling on top.

Compaction shadow-vs-live study (`scripts/compaction_study.py`, same verbose fixture conversation,
`target_tokens=1200` to force the pipeline):

| metric | shadow | live |
|---|---|---|
| run status / fixture tests | submitted / pass | submitted / pass |
| compaction runs | 12 | 12 |
| judge source | laya | laya |
| tokens (last run) before → after | 10,598 → 2,784 | 10,742 → 3,674 |
| verdicts (keep/drop/shorten, last run) | 31/0/4 | 23/0/4 |
| deterministic drops + layer-3 fitting | 6 items | 6 items |
| degradations | none | none |

Live pruning is applied only after this shadow comparison: the fixture fix still passes, the pins
(system/task/credentials/failing evidence/current file snapshots) and the recency window survived, and
the model was told about the omission via the `<compaction>` marker. With production thresholds
(`target_tokens: 60000`, `recent_tokens: 12000`) compaction only engages on long runs.

## Testing

```sh
make test          # 168 unit tests (no model calls) + mock-endpoint E2E on the fixture repo
make test-live     # the same E2E against the evaluator endpoint (needs credentials)
make smoke         # trivial live task: create hello.txt containing done
```

The E2E copies `tests/fixture-repo/` (Python off-by-one bug + failing test) into a temp git repo,
drives the harness with either a scripted deterministic model (`--dry-run`) or a local
OpenAI-compatible mock server, and asserts: the agent submits, the fixture tests pass afterwards,
the report exists, the patch captures the fix, telemetry recorded model calls, and **no runtime
credential appears in any run artefact** (trajectory included). Live mode runs the
same assertion against the real endpoint.

## Limitations (honest)

* The upstream agent is **bash-only** by design; our "tools" are sentinel commands, not new tool
  schemas. Edits happen through shell commands (sed/heredoc), exactly as upstream intends.
* Cost is only reported when the endpoint's price table is known; otherwise the report shows
  provider-reported tokens and `not reported (unknown price table)`.
* Scope rollback needs a git checkout; non-git workspaces get reporting only.
* laya judgements are CPU-bound (~0.1–1 s per batch on an M-series laptop) and, as the appendix
  shows, domain-weak zero-shot. They are used where evidence supports them and gated conservatively.
* The harness refuses network commands by policy; a task that genuinely requires fetching packages
  will report that blocker rather than silently ignore it.

## Attribution

Built on **mini-swe-agent**, MIT, © the SWE-agent team (Kilian A. Lieret, Carlos E. Jimenez).
The original license text is kept verbatim in `LICENSE`; the pinned upstream source is vendored in
`vendor/mini-swe-agent/` and is **not modified** (`make check-upstream` verifies byte-identity with
tag v2.4.6). The local decision model is **laya** (Apache-2.0, Convai Innovations), used as an
optional sidecar; its license and documentation live with the package.
