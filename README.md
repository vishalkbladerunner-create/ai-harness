<div align="center">

# ⬡ guarded-mini

### A guarded, evidence-first SWE harness with a local calibrated decision layer

![tests](https://img.shields.io/badge/tests-185%20passing-brightgreen)
![python](https://img.shields.io/badge/python-%E2%89%A5%203.10-blue)
![upstream](https://img.shields.io/badge/mini--swe--agent-v2.4.6%20pinned-orange)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

**Same model. Different harness. The engineering is the difference.**

*Built by team **Neuromancer** — Vishal Kumar · Manav Garg · Karunesh Mahra*

Built on [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) (MIT, © the SWE-agent team),
pinned at **v2.4.6** and vendored byte-identical under `vendor/mini-swe-agent/`.

</div>

---

## What is this?

An **AI coding harness**: everything that surrounds a foundation model so it can do real
software-engineering work autonomously — read a GitHub issue, navigate a repository, edit code,
run tests, recover from mistakes, and submit a **verified** patch.

The thesis in one line:

> The frontier model (DeepSeek/Qwen) does the software engineering.
> **The harness does everything else** — safety, context, verification, recovery, accounting, evidence —
> and every decision it makes is auditable in a run report.

A small local classifier (**laya**, ~421M params, CPU-only, optional) assists with *infrastructure*
judgments — "is this command risky?", "is this context still needed?" — using calibrated
probabilities. It never does task work and the harness runs fine without it.

---

## Quickstart — the evaluator path (2 minutes)

```sh
git clone <this repo> && cd <repo>

export AI_API_KEY="<PROVIDED_KEY>"     # the ONLY variable required

make setup      # .venv + pinned vendored core + harness (+ optional laya/TUI deps)
make doctor     # optional: probes your key against DeepSeek & Qwen (free, no tokens billed)
make run        # interactive: the TUI asks for the issue — paste it, press Enter
                # scripted:    make run < issue.md   (stays headless — the committee path)
```

That is the whole contract. Python ≥ 3.10 is required; if the machine only has an older one,
`make setup` provisions a project-local CPython automatically via `uv`.

<details><summary>More ways to run</summary>

```sh
make run ISSUE=issue.md                          # issue as a file path (same run, no pipe)
make run WORKSPACE=/path/to/target-repo < issue.md
make run BUDGET=20 ARGS="--max-steps 60" < issue.md   # evaluators can impose caps
make run TUI=1 ARGS="--dry-run"                  # TUI demo, no credentials, no cost
make replay                                      # reopen the last run in the TUI
make test                                        # 190 unit tests + mock-E2E (no API calls)
make test-live                                   # one live E2E pass (spends your AI_API_KEY)
make smoke                                       # one trivial live task
make clean                                       # remove venv, caches, generated reports
```

</details>

**How the target repository is found** (first hit wins): `WORKSPACE=`/`--workspace` → a
`Workspace: /path` line in the issue → a git checkout whose absolute path appears in the issue →
the current directory → the GitHub repo named in the issue (cloned to `.cache/workspaces/`) →
the current directory with a warning. If the only candidate is the harness's **own checkout**, the
run refuses with clear instructions instead (running the agent on its own source is unsafe;
`--force` overrides).

---

## The interface

On an interactive terminal, `make run` opens the **collect screen** — paste the issue, press
**Enter** — then the live view: the same headless run, rendered.

```
┌─ guarded-mini ─────────────────────────────────────────────── run 20260927-… ── ⬡ Graph ─┐
│ running · step 7 · tok 12,400 (in 10,100 · cache 6,300 · out 2,300) · wall 38s · live     │
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

| panel | what it shows |
|---|---|
| **Status line** | run state, step counter, **token summary — input / cache-hit / output**, wall clock, model, mode |
| **Context split** | what the next request is made of (system / tools / messages / free) + cumulative compaction savings |
| **Project graph** | ground truth of what the run touched, rebuilt from `git` every 2 s — independent of telemetry |
| **Agent stream** | every event as written: model calls, guardrail decisions with calibrated probabilities, verification PASS/FAIL, compaction verdicts, budget notices |

Design rule: the TUI is a **viewer**. The evaluation path is headless; the TUI launches the *same*
entrypoint as a child process and tails its telemetry, so the visual path and the evaluation path
can never drift apart. Commands work like other agent harnesses: type **`/`** and the command
palette opens (`graph`, `follow`, `quit`) — Enter runs the highlighted command; `g` / `f` / `q` /
`Ctrl-Q` remain as shortcuts. If TUI dependencies are
missing it explains itself and the headless path is unaffected.

---

## Architecture

Five layers, one direction of flow, one choke point for every side effect:

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ ① INTERFACE                              make setup / make run / make test      │
│    interactive → TUI collect screen (paste + Enter)                             │
│    scripted    → stdin / ISSUE=path        (headless — the committee path)      │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│ ② ORCHESTRATION                     harness/run.py + HarnessAgent               │
│    workspace resolution → repo map → agent loop (vendored mini-swe-agent core)  │
│    per step: budget check → compaction view → model call → action guard gate    │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                        ▼
              ┌──────────────────────────────────────────────────────┐
              │ ③ FOUNDATION MODEL — the only task brain              │
              │   DeepSeek deepseek-flash (default)                   │
              │     └─ auth/model failure → Qwen (automatic fallback) │
              │   OpenAI-compatible · text-only · pinned temp/seed    │
              └──────────────────────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│ ④ GUARD LAYER                       GuardedEnvironment — one choke point        │
│    deterministic policy → laya grey-zone second opinion → bash execution →      │
│    verification block → injection scan → secret masking → telemetry             │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                        ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│ ⑤ EVIDENCE                          telemetry.jsonl → REPORT.md + patch.diff    │
│    every model call, command, decision, verdict — with probabilities            │
│    the live TUI tails this same stream; reports/LATEST tracks the newest run    │
└────────────────────────────────────────────────────────────────────────────────┘

        ╎ sidecar: laya — local calibrated judge (421M, CPU, optional). It answers
        ╎ small typed infrastructure questions only; it is never the task model,
        ╎ and every layer degrades to documented heuristics when it is absent.
```

| layer | lives in | built on |
|---|---|---|
| ① Interface | `harness/entrypoint.py`, `harness/tui.py`, `Makefile` | Textual |
| ② Orchestration | `harness/run.py`, `harness/agent.py`, `harness/budget.py` | vendored mini-swe-agent |
| ③ Model access | `harness/model.py`, `harness/config/` | litellm (upstream) |
| ④ Guard layer | `harness/environment.py`, `harness/guardrails/`, `harness/verify.py` | deterministic rules + laya |
| ⑤ Evidence | `harness/telemetry/`, `harness/report.py`, `harness/secrets.py` | append-only JSONL |

---

## What we built

### 🛡 Guardrails — 8 layers, deterministic first

Every command passes `GuardedEnvironment.execute → ActionPolicy.check` **before bash sees it**.

1. **Deterministic rules** (authoritative, `harness/config/policy.yaml`): privilege escalation,
   filesystem destruction, credential access, network egress, git staging/history, writes outside
   the workspace — with real write-target extraction (quote-aware shell parsing, not text matching).
2. **Workspace-confined writes pass without judging** — that is the agent's normal work; scope is
   checked on the final diff instead.
3. **Dynamic payloads refused deterministically** (`python -c …rmtree…`, `base64 -d | sh`): a
   dangerous primitive inside an opaque payload is never delegated to the judge.
4. **Grey zone → laya second opinion**: typed `safe/destructive/out_of_scope` + severity, one
   batched forward pass, calibrated temperature, bands `act < 0.70 ≤ ask < 0.90 ≤ refuse`.
5. **Unattended `ask` = refuse-with-guidance** — the model gets the reason, the probability, and a
   safe alternative, then continues. The deny path is graceful, never fatal.
6. **Scope guard**: end-of-run diff classification; deterministic out-of-scope changes are rolled
   back (git workspaces only), judge-only flags are excluded from the submitted patch but never
   reverted — a wrong rollback is worse than a reported oddity.
7. **Injection defence**: repository content is data, never instructions. Pattern scan + laya probe;
   flagged observations carry a `<quarantine>` warning the compactor can never drop.
8. **Secret hygiene**: credential-like env values are *blanked* in the child environment, every
   artefact passes a masking choke point, the model config is redacted before serialization — and a
   regression test asserts no key material ever lands in a run artefact.

### 🧠 Calibrated compaction — long runs without losing the plot

Three strict layers: **deterministic preparation → laya semantic selection → deterministic
fitting**. Token maths in code, never by the judge.

* Causal items (a call and its tool results are never severed); pins for system/task text,
  credentials, failing-test evidence, quarantine blocks, and the newest snapshot of every
  issue-referenced file; the most recent ~12k tokens are exempt.
* One **batched** judge call per pass; verdicts `keep/drop/shorten` gated by
  `P(drop) ≥ 0.60 AND P(essential) < 0.50` with a staleness rescue and a keep-by-default policy.
* The canonical trajectory is never modified — compaction only changes what the model *sees*.
* `shadow` mode judges-and-logs without pruning; live mode was enabled only after a recorded
  shadow-vs-live study (see the calibration appendix).

### ✅ Self-verification — evidence, not claims

* After any workspace-changing command, the detected test command (`pytest` / `npm test` /
  `make test`) runs and its result is appended to the observation as a
  `<verification status="PASS|FAIL">` block — that block *is* the feedback loop.
* `harness_submit_patch` ends the run with the final scoped diff + recorded verification
  (a sentinel command; bash never sees it). A bounded repo map is injected at session start.

### 🔌 Provider discovery & fallback — only the key is required

With just `AI_API_KEY` exported, the harness tries the checked-in chain —
**DeepSeek `deepseek-flash` → `deepseek-v4-pro` → Qwen `qwen3.7-plus` → `qwen3.8-max`** — and
discovers which official provider the key belongs to (one switch on 401/404, same-provider models
skipped on auth failures, every switch recorded in telemetry). If **both** reject the key, the run
stops with a plain message naming the supported official APIs. Explicit `MODEL_BASE_URL` +
`MODEL_NAME` (e.g. the committee's prescribed model) always win verbatim and disable the fallback —
this is provider *discovery*, never model *substitution*. `make doctor` probes both endpoints with
your key beforehand, free.

### ⏱ Budgets as safety nets, not task limits

Following the posture of the widely used agent harnesses (OpenCode, Kimi Code, Pi), **the model
runs until it decides the task is done**. Every step/call/token/wall-clock budget is opt-in
(`0` = unlimited); only token nets carry defaults, sized far above any plausible single task.

* At 80% of an enabled budget the agent receives an in-context wrap-up notice; at 100% a graceful
  `LimitsExceeded` exit with partial results in the report.
* A **stall detector** guards unlimited runs: identical action 5× in a row → in-context nudge;
  10× → graceful "stuck" exit. This — not a raw step cap — terminates pathological loops.
* Evaluators impose caps without touching source: `make run BUDGET=20`, `ARGS="--max-steps 60"`.

### 📊 Token & cache accounting

Per-call usage is normalized (with a labelled char-estimate fallback) and accumulated: prompt,
completion, total — and **cache-hit tokens** (DeepSeek `prompt_cache_hit/miss_tokens`; OpenAI
nested shape supported), shown live in the TUI status line and in telemetry. Cache hits bill ~50×
cheaper, and the harness's stable prompt prefix makes most input tokens cache hits (measured: 91%
on a live smoke run).

### 📁 Telemetry & the report

`reports/<run-id>/` = `REPORT.md` + `telemetry.jsonl` + `trajectory.json` + `patch.diff` +
`status.json`. The report is the judging artefact: issue, summary, context window, reproducibility
(pinned temperature/seed), files changed with the diff, verification evidence, guardrail log with
probabilities, scope check, injection scans, compaction audit, degradation notes, budget, timeline.

---

## Live evidence (2026-09-27, DeepSeek `deepseek-flash`)

**SWE-bench Verified: 2/2 solved.** Real benchmark instances, cloned at their base commits, the
problem statement fed verbatim through the standard `make run` path, scored with the benchmark's
own test patches (both FAIL_TO_PASS tests confirmed failing on the base commit first):

| instance | result | patch |
|---|---|---|
| `sympy__sympy-22914` (PythonCodePrinter Min/Max) | ✅ `test_PythonCodePrinter` passes (20/20 file tests) | character-identical to the gold patch |
| `sympy__sympy-23950` (`Contains.as_set`) | ✅ `test_as_set` passes (6/6 file tests) | same file + equivalent change as gold |

Benchmark cost: **$0.056** total (40 + 25 model calls; 96–97% of the ~1.3M input tokens were
cache hits, verified against the provider payload).

Produced with a real funded key, total cost ≈ **$0.018**:

| run | result |
|---|---|
| `make doctor` | key authenticated; `deepseek-flash` advertised by the account |
| `make smoke` | live model completed the task via tool calling and submitted |
| `make test-live` | the model **fixed a real failing test** (off-by-one), the verification loop ran pytest green twice, patch captured, guardrails + injection scans active, **no credential material in any artefact** |
| usage accounting | 14,080 of 15,503 input tokens were **cache hits (91%)** — the token/cache summary was verified against the provider payload |

Earlier full-environment checks: fresh-copy `make setup` + `make test` PASS (both with and without
laya), vendored core byte-identical to upstream v2.4.6 (`make check-upstream`). The log lives in
`docs/ARCHITECTURE.md` §8.

---

## Hackathon compliance map

| requirement (submission guidelines) | how this repo meets it |
|---|---|
| `Makefile` at repo root with `setup` / `run` / `test` / `clean` | ✅ all four (+ `doctor`, `smoke`, `replay`, `check-upstream`, `check-clean-env`) |
| credential via `AI_API_KEY` env var only | ✅ read once by `harness/config.read_env()`, never written to disk; masked in every artefact; regression-tested |
| no hard-coded secrets anywhere | ✅ none — source, Makefile, docs and `.env.example` (empty values) are clean |
| text-only models | ✅ OpenAI-compatible chat completions only; no multimodal code paths |
| model configuration clearly defined | ✅ `harness/config/harness.yaml` — provider chain, pinned `temperature: 0.0` / `seed: 42`, budgets |
| prescribed model must be usable | ✅ explicit `MODEL_BASE_URL`/`MODEL_NAME` win verbatim and disable fallback |
| TUI launchable through `make run` | ✅ interactive terminals get the TUI automatically; piped runs stay headless |
| environment independence | ✅ `make setup` handles Python provisioning, pins, and optional deps non-fatally; no manual steps |
| reproducible execution | ✅ pinned sampling params + seed, `constraints.txt` pins, degradations recorded when an endpoint rejects a parameter |
| evaluator needs no team assistance | ✅ `git clone` → `export AI_API_KEY` → `make setup` → `make run`; nothing else |

**Repository layout, matching the submission checklist:**

```
.
├── Makefile                 # the standardized interface (REQUIRED)
├── README.md                # this file
├── harness/                 # source: entrypoint, orchestration, guardrails,
│   ├── guardrails/          #   policy, scope, injection, judgments
│   ├── compaction/          #   3-layer calibrated compaction
│   ├── telemetry/           #   append-only event log + masking
│   ├── laya/                #   local judge adapter + heuristics fallback
│   └── config/              # configuration: YAML, prompts, calibration (no secrets)
├── vendor/mini-swe-agent/   # vendored upstream core, byte-identical to v2.4.6
├── tests/                   # 190 unit tests + fixture repo + mock model server
├── scripts/                 # setup, e2e (mock+live), smoke, doctor, calibration
├── docs/ARCHITECTURE.md     # line-by-line walkthrough of the loop and every hook
├── constraints.txt          # dependency pins (tested)
└── pyproject.toml           # dependency + package declaration
```

---

## Ours vs upstream — the honest split

| path | owner | what it is |
|---|---|---|
| `vendor/mini-swe-agent/` | **upstream**, unmodified | the agent loop, bash tool, litellm model, local environment |
| `harness/` + `scripts/` + `tests/` | **ours** | entrypoint, guardrails, compaction, telemetry, TUI, reporting, laya adapter |
| `harness/config/` | ours | all checked-in configuration + prompt overlay + calibration |
| `docs/`, `NOTES-BUILD.md` | ours | architecture walkthrough, build log |
| `LICENSE` | upstream, verbatim | MIT, © 2025 Kilian A. Lieret and Carlos E. Jimenez |

We never patch upstream. Contact happens at four documented seams: `HarnessAgent(DefaultAgent)`,
`HarnessModel(LitellmModel)`, `GuardedEnvironment(LocalEnvironment)`, and prompt templates
*extended* from the vendored `mini.yaml`. `make check-upstream` proves byte-identity.

---

## Calibration appendix (real numbers, honest reading)

<details><summary>laya temperature fitting + compaction study</summary>

`scripts/calibrate_judge.py` fits one temperature per bucket on 64 labelled shadow-mode items
(`tests/fixtures/judge_items.jsonl`), clamped to laya's documented `[0.5, 5.0]`:

| bucket | n | ECE before | ECE after | fitted T | accuracy @0.5 |
|---|---|---|---|---|---|
| `action_risk` (guardrail risk) | 28 | 0.209 | 0.200 | 0.522 | 0.68 |
| `drop` (compaction droppable) | 15 | 0.305 | 0.220 | 0.500 (clamp) | 0.80 |
| `essential` (compaction keep) | 15 | 0.417 | 0.312 | 5.000 (clamp) | 0.47 |
| `in_scope` (scope guard) | 9 | 0.371 | 0.216 | 4.933 | 0.33 |
| `injection` (probe) | 12 | 0.233 | 0.203 | 0.725 | 0.75 |

**Honest reading:** the base laya checkpoint is weak zero-shot on these domain questions. The
fitted temperatures make that explicit — `essential`/`in_scope` are flattened toward 0.5, which is
exactly why compaction keeps everything borderline and scope rollback always requires a
deterministic anchor. The judge earns its place where the evidence is better (`drop`, `injection`,
`action_risk`); the deterministic layer carries the load elsewhere. laya also warns at load that
the shipped checkpoint contains an invalid temperature for one high-cardinality bucket; we record
the warning and fit our own scaling on top.

Compaction shadow-vs-live study (same verbose fixture conversation, forced pipeline):

| metric | shadow | live |
|---|---|---|
| run status / fixture tests | submitted / pass | submitted / pass |
| tokens (last run) before → after | 10,598 → 2,784 | 10,742 → 3,674 |
| verdicts (keep/drop/shorten) | 31/0/4 | 23/0/4 |
| degradations | none | none |

</details>

---

## Limitations (honest)

* The upstream agent is **bash-only** by design; our extra "tools" are sentinel commands, not new
  tool schemas. Edits happen through the shell, exactly as upstream intends.
* laya judgements are CPU-bound and, as the appendix shows, domain-weak zero-shot — used where the
  evidence supports them, gated conservatively everywhere else.
* Scope rollback needs a git checkout; non-git workspaces get reporting only.
* Network commands are refused by policy; a task that genuinely requires fetching packages will
  report that blocker rather than silently route around it.
* Cost is only reported when the endpoint's price table is known; otherwise the report shows
  provider-reported tokens (including cache hits) and marks cost as not reported.

## Attribution

Built on **mini-swe-agent**, MIT, © the SWE-agent team (Kilian A. Lieret, Carlos E. Jimenez) —
license kept verbatim in `LICENSE`, source vendored unmodified in `vendor/mini-swe-agent/`.
The local decision model is **laya** (Apache-2.0, Convai Innovations), used as an optional sidecar.
