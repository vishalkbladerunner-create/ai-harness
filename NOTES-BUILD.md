# NOTES-BUILD.md — live build log (what works, what broke, exact errors)

## Decisions locked at recon (2026-09-27)

| decision | value | why |
|---|---|---|
| base | mini-swe-agent **v2.4.6** = `a83fcae82d2a08f0ee0c688f9d137b3566c097f8` | user decision after NOTES.md turned out to have no mini-swe-agent pin recorded |
| vendoring | full source under `vendor/mini-swe-agent/`, pristine (no local edits) + `PINNED` file | compliance 5 (self-contained), reproducible pin, clean ours/theirs separation |
| python | venv built with **python3.12** (bootstrap prefers 3.13→3.10) | torch/litellm wheel availability; Makefile falls back to `python3` |
| live verification | mock OpenAI-compatible server for E2E | `AI_API_KEY`/`MODEL_BASE_URL`/`MODEL_NAME` are **not** exported in the tool shell (same finding as recon NOTES.md) |
| cost accounting | `cost_tracking: ignore_errors` + own token budget | unknown price tables on evaluator endpoints must not crash runs |
| upstream purity | `make check-upstream` diffs the vendor against a fresh clone at tag v2.4.6 | verified byte-identical (only our `PINNED` file differs) |

## Phase 0 — skeleton + evaluation interface

### Working (verified)

```text
.venv/bin/python -m pytest tests/unit -q
  -> 193 passed (final state after all phases + the TUI collect mode, provider
     refresh, budget safety-net redesign, and token/cache accounting; 35 at the end of Phase 0)

.venv/bin/python scripts/e2e_fixture.py --mode mock
  -> [e2e] RESULT: PASS  (submitted / fixture tests pass / report / patch / telemetry)

printf 'Create a file hello.txt containing done\n' | make run WORKSPACE=/tmp/mk-test ARGS="--dry-run"
  -> status: submitted (Submitted); report + patch written under reports/
```

`reports/<run-id>/` contains `REPORT.md`, `telemetry.jsonl`, `trajectory.json`, `patch.diff`, `status.json`.
`reports/LATEST` points at the newest run.

### Broke / fixed (exact errors)

1. **`pip install -e .` hung indefinitely.** `setuptools.packages.find` walked `harness-lab/` (cloned
   reference harnesses, node_modules). Fix: explicit `[tool.setuptools] packages = [...]`. Install now <1s.
2. **Upstream import-time home-directory dependency.** `minisweagent/__init__.py` creates and
   dotenv-loads `platformdirs.user_config_dir("mini-swe-agent")` and prints a startup banner. Fix
   without patching: `harness/__init__.py` sets `MSWEA_GLOBAL_CONFIG_DIR=<repo>/.cache/mini-swe-agent`
   and `MSWEA_SILENT_STARTUP=1` before any `harness.*` import; also pins `HF_HOME` project-local.
3. **`Telemetry.emit() got multiple values for argument 'kind'`.** Fix: `def emit(self, kind, /, **fields)`.
4. **Payload field named `kind` silently shadowed the event type** (produced `"kind": ""` events).
   Fix: colliding payload fields are renamed `reported_kind`; call sites use `test_kind`.
5. **Mock responses were mangled** (naive `'`→`"` replacement broke `sed 's/…/…/'`), which raised an
   upstream `FormatError` that consumed the next scripted response. Fix: `json.dumps({"command": ...})`.
6. **Secret stripping was ineffective**: upstream `LocalEnvironment.execute` runs `os.environ | config.env`,
   so *omitting* a variable does not remove it. Fix: explicitly blank every credential-like env name.
7. **Reports contained raw issue text.** Fix: `render_report()` masks credential-like strings.

## Phase 1 — guardrails

### Working (verified)

```text
tests/unit/test_policy.py        -> deterministic refusals, grey-zone judge bands, shadow mode
tests/unit/test_scope.py         -> anchor rule, git rollback, untracked deletion
tests/unit/test_injection.py     -> pattern + judge flags, quarantine blocks
tests/unit/test_guardrails_e2e.py-> real run: rm outside workspace + .env read refused,
                                    POLICY REFUSAL observations reach the model, run continues
.venv/bin/python scripts/calibrate_judge.py
  -> action_risk T=0.522 (ECE .209->.200) | drop T=0.500 (.305->.220) | essential T=5.0 (.417->.312)
     in_scope T=4.933 (.371->.216) | injection T=0.725 (.233->.203)
```

### Broke / fixed (exact errors)

1. **laya refused `printf … > harness_dry_run_marker.txt` as "destructive" (p=0.786).** Diagnosis: the
   base checkpoint is weak zero-shot (its own docs say so) and my grey zone was too wide. Fix (design,
   not threshold fudging): writes statically confined to the workspace are deterministic *allow*; the
   grey zone is reserved for unconfined/dynamic commands. Thresholds raised to act<0.70≤ask<0.90≤refuse.
2. **Sed expressions were mistaken for filesystem paths** (`/items[len(items)\]/` → "writes outside
   workspace", refusal). Fix: `extract_write_targets()` only inspects redirection targets and
   write-verb arguments; quoted sed scripts are excluded.
3. **Scope rollback could have reverted the actual fix** for issues that don't name the buggy file.
   Fix: rollback requires a deterministic *anchor* (at least one in-scope change); otherwise report-only.
4. **A payload field named `kind`** (again) — hardened once, in telemetry.

## Phase 2 — calibrated compaction

### Working (verified)

```text
tests/unit/test_compaction.py    -> pairing, pins, recency, duplicates, verdict bands,
                                    staleness rescue, shadow pass-through, fallback, batching
.venv/bin/python scripts/compaction_study.py
  -> shadow: 10,598 -> 2,784 tokens (12 runs, verdicts 31 keep / 0 drop / 4 shorten)
     live:   10,742 -> 3,674 tokens; fixture tests pass in BOTH modes
     judge=laya, no degradations, pins (system/task/credentials/failing evidence) + recency survived
```

### Notes

* The fitted `essential`/`in_scope` temperatures clamp at 5.0, i.e. the judge is near-uninformative on
  those two buckets on our fixtures. The policy therefore relies on the *conjunction* (drop verdict
  AND low essential probability AND staleness) and keeps everything borderline — documented in the
  README appendix instead of hidden.
* `drop` fits to T=0.5 (clamped low, i.e. sharpening), accuracy 0.80: the bucket where the judge earns
  its place.
* Duplicate elimination and layer-3 fitting do the bulk of the pruning; the judge's removals are rare
  and conservative, exactly as the brief requires.

### Broke / fixed

1. **Items were never judged** (`candidates` empty): `droppable` was only set for duplicates. Fix:
   set it for every non-pinned, non-recent, non-tiny item.
2. **Duplicate detection never fired** because item text included the assistant's reasoning. Fix: compare
   tool-result text only.
3. **Over-pinning**: any item whose *command* mentioned a scope term was pinned. Fix: pin only the
   *newest* snapshot per referenced file (older snapshots are stale and judgable).

## Phase 3 — self-verification + sentinels

### Working (verified)

```text
tests/unit/test_verification.py  -> fingerprint change detection (content-sensitive), FAIL blocks,
                                     no duplicate runs of the model's own test command, on/off modes
tests/unit/test_verification_e2e.py -> wrong fix → <verification status="FAIL"> fed back →
                                       corrected fix → PASS → harness_submit_patch submits with evidence
tests/unit/test_sentinels.py     -> submit_patch first line is COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT,
                                     patch written, verification run, no-tests case reported
```

### Broke / fixed

1. **Sentinels bypassed the submission mechanism.** `_check_finished` lives inside upstream's
   `LocalEnvironment.execute`, which the sentinel path skipped, so `harness_submit_patch` ended the
   step but not the run. Fix: call `self._check_finished(final)` after finalising sentinel output.
2. **`git status` is not content-sensitive**: correcting an already-modified file produced an identical
   porcelain line, so re-verification was skipped. Fix: fingerprint = sha1(porcelain + `git diff`).
3. **`__pycache__` leaked into patches** (untracked files after pytest runs). Fix: cache/artefact
   ignore patterns applied in `collect_changes`.
4. Removed a 2 s verification debounce that hid legitimate re-verification.

## Phase 4 — telemetry + report

* `model_call` (latency/tokens/cost), `command` (decision + policy payload), `guardrail`
  (outcome/probability/temperature), `compaction` (full per-item verdicts), `verification`,
  `injection_scan`, `degradation`, `budget_exhausted`, `run_start`/`run_end`.
* REPORT.md sections: header/provenance, Issue, Summary (+provider cost), Reproducibility
  (temperature/seed/upstream pin/calibration), Files changed, Verification evidence, Guardrail log
  (+ decision-layer status), Scope check, Injection defence, Compaction audit, Degradation notes,
  Budget, Timeline, Submission.

## Phase 5 — persona + CLI

* ASCII banner in the entrypoint and the report header; upstream templates *extended* from
  `harness/config/prompts/{system,instance}_extension.md` (never replaced).
* Flags: `--issue/--verify/--report/--budget` plus `--max-steps`, `--dry-run`, `--scenario`, `--json`,
  `--force`, `--workspace`. No TUI (optional per brief; not needed for the evaluation path).

## Phase 6 — tests + clean-environment verification

```text
make test        -> 110 unit tests + mock-endpoint fixture E2E: PASS
make check-upstream -> vendored core == upstream v2.4.6 (byte-identical)
scripts/clean_env_check.sh --skip-laya  -> fresh copy → make setup → make test: PASS
scripts/clean_env_check.sh              -> fresh copy incl. laya install + checkpoint: PASS
make smoke / make test-live             -> BLOCKED here: credentials are not exported in the tool
                                           shell. The user runs them once the evaluator's env vars are
                                           exported; mock E2E proves the same code path.
```

## Phase 7 — live TUI + context instrumentation (optional presentation layer)

### Working (verified)

```text
tests/unit/test_theme.py / test_context_split.py / test_tui.py -> 18 tests (wordmark, split
  arithmetic, telemetry tail, widgets, headless Textual replay + live-quit)
.venv/bin/python scripts/e2e_fixture.py --mode mock
  -> model_call events now carry context_split (system 513 / tools 137 / messages 1415 of 65536);
     REPORT.md gains a "## Context window" section
headless live-TUI check (Textual run_test, real child entrypoint, --dry-run)
  -> child rc 0, status submitted, split tailed live, graph showed the touched file
PTY check of `make run TUI=1` -> renders, q quits, exit 0 (screenshot-ish capture in the log)
```

* `harness/theme.py` — brand palette + Neuromancer wordmark (47 cols, 2 lines), used by the CLI
  banner, the TUI and the Markdown report header.
* `harness/context/tokens.py` — tiktoken cl100k split (system / tools / messages / free) with a
  chars/4 fallback; the tools bucket is upstream's real bash schema plus the sentinel docs section
  of the task prompt. Context window comes from `model.context_limit` in `harness.yaml`
  (per-provider overrides: DeepSeek 64K, Qwen 128K).
* `harness/tui.py` + `harness/tui_widgets.py` — Textual viewer over telemetry.jsonl: context
  panel, agent stream, live git graph, ⬡ toggle. `make run TUI=1` and `make replay RUN=…`.

### Broke / fixed (exact errors)

1. **`python harness/entrypoint.py` crashed in litellm with `AttributeError: module 'secrets' has no
   attribute 'token_hex'`.** Script mode put `harness/` on `sys.path[0]`, so litellm's `import
   secrets` resolved to our `harness/secrets.py`. Fix: the entrypoint drops its own directory from
   `sys.path` and inserts the repo root (module mode was already correct).
2. **In the live TUI, keyboard input died once the child run finished (pty write → `EIO`).** The
   child harness inherited the TUI's terminal stdin, and its bash runner calls `setsid()`
   (`start_new_session`), which hangs up the inherited pty. Fix: spawn the child with
   `stdin=DEVNULL` — it gets its task via `--issue` and never reads stdin. Reproduced without
   Textual (plain parent + child + pty), then fixed and re-verified under a pty.
3. **`q` mid-run reported the child's `-15` as the exit code.** Fix: `_on_child_exit` keeps the
   interruption code (130) when the user quit first.

## Blocked

* **Live endpoint runs (`make smoke`, `make test-live`)** — `AI_API_KEY` / `MODEL_BASE_URL` /
  `MODEL_NAME` are empty in the build shell (same finding as the recon notes). Everything that can be
  verified without credentials is verified: the same code path runs against the local
  OpenAI-compatible mock in `make test`, and the clean-environment checks pass.
