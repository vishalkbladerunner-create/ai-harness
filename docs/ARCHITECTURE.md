# ARCHITECTURE — guarded-mini

> Faculty-round companion. It answers, with line references, **what happens between the model
> returning a tool call and the tool executing**, **where every layer hooks in**, and **what happens
> when the local judge fails**. Sections marked _(later phase)_ are completed when that phase lands;
> the build order is in `NOTES-BUILD.md`.

## 0. Ours vs upstream — the one-word answer

| path | owner | contents |
|---|---|---|
| `vendor/mini-swe-agent/` | upstream (MIT, © SWE-agent team) | the entire agent core, pinned v2.4.6, **unmodified** |
| `harness/` | **ours** | entrypoint, guardrails, compaction, telemetry, config, laya adapter |
| `scripts/`, `tests/`, `docs/` | ours | setup/E2E/smoke, tests + fixture, this document |
| `Makefile`, `README.md`, `LICENSE` | ours (LICENSE is upstream text, verbatim) | evaluation interface + attribution |

Nothing outside `harness/`, `scripts/`, `tests/` and `docs/` is our code. The only upstream files we
read at runtime are its built-in prompt templates (`minisweagent/config/mini.yaml`), which we *extend*.

## 1. Process model

```
make run
 └─ python -m harness.entrypoint          # CLI: task from stdin/--issue, flags
     └─ harness.run.execute_run(RunOptions)
         ├─ harness.config             # checked-in YAML + env (env read ONCE, never written)
         ├─ GuardedEnvironment         # execution choke point
         ├─ HarnessModel               # litellm wrapper (accounting, param fallback)
         ├─ HarnessAgent(DefaultAgent) # upstream loop + budgets + compaction view
         │    └─ loop: query() → execute_actions()
         ├─ harness.verify.Verifier     # self-verification evidence
         └─ harness.report              # REPORT.md from telemetry + trajectory
```

## 2. The upstream loop, line by line

`vendor/mini-swe-agent/src/minisweagent/agents/default.py`

| lines | what happens | our involvement |
|---|---|---|
| `run()` 88–124 | initialises `self.messages` with a `system` message (rendered `system_template`) and a `user` message (rendered `instance_template` with `{{task}}` and friends); loops; catches `InterruptAgentFlow` (adds exit message) and `FormatError` (counts consecutive format errors; exits with `RepeatedFormatError` after N); saves the trajectory after **every** step in `finally` | we subclass `HarnessAgent` and add template vars (`repo_map`, `issue_meta`) via `extra_template_vars` |
| `step()` 126–128 | `execute_actions(self.query())` — one model call, then its actions | — |
| `query()` 130–152 | checks `step_limit` / `cost_limit` / `wall_time_limit_seconds` (raising `LimitsExceeded` / `TimeExceeded`), increments `n_calls`, calls `self.model.query(self.messages)`, adds cost, appends the assistant message | **overridden** in `HarnessAgent.query`: budget check, compaction view, per-step telemetry |
| `execute_actions()` 154–157 | `[self.env.execute(a) for a in message.extra.actions]`, then `model.format_observation_messages(...)` appends one `tool` message per action | env is our `GuardedEnvironment`; observations optionally carry our postprocessor blocks |
| `serialize()` 159–180 | builds the trajectory dict: `info` (model stats, config, exit status, submission) + `messages` | **overridden** to add a `harness` info block |
| `save()` 182–189 | writes that dict to `output_path` (every step) | we point `output_path` at `reports/<run>/trajectory.json` |

`vendor/mini-swe-agent/src/minisweagent/models/litellm_model.py`

| lines | what happens |
|---|---|
| `query()` 81–106 | `litellm.completion(model, messages, tools=[BASH_TOOL], **model_kwargs)`; extracts `actions` via `_parse_actions`; returns the assistant message with `extra={actions, response, cost, timestamp}`; on `FormatError` persists the response + cost on the error |
| `_parse_actions()` 128+ | `parse_toolcall_actions` (see below); raises `FormatError` for unknown tool / bad args / no tool call |
| `_calculate_cost()` 108–126 | litellm cost table; raises unless `cost_tracking: ignore_errors` — we set `ignore_errors` (unknown evaluator endpoints) and budget on tokens instead |

`vendor/mini-swe-agent/src/minisweagent/models/utils/actions_toolcall.py`

* `BASH_TOOL` (line 11): the **only** tool — `bash(command: string)`. mini-swe-agent is deliberately bash-only; our "tools" are sentinel commands, not new tool schemas.
* `parse_toolcall_actions` (line 30): turns `tool_calls[]` into `[{command, tool_call_id}]`.
* `format_toolcall_observation_messages` (line 79): renders each output through `observation_template` into a `role: tool` message **and copies `output.extra` into `message.extra.raw_output`**.

`vendor/mini-swe-agent/src/minisweagent/environments/local.py`

* `execute()` (line 24): merges `os.environ | self.config.env` and runs the command with `subprocess.Popen(shell=True, start_new_session=True)`, cwd pinned, timeout enforced.
* `_run()` (line 72): on timeout kills the **whole process group** (`os.killpg`) — the base of our runaway-process story.
* `_check_finished()` (line 45): if the first output line is exactly `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, raises `Submitted(submission=rest_of_output)`; `DefaultAgent.run` turns that into the exit message. This is the documented submission mechanism we reuse.

## 3. Our interception points, in execution order

1. **Build time** — `harness/run.execute_run`: workspace guard (`resolve_workspace` refuses the harness
   repo itself), run directory, telemetry, repo map, budget, prompts, model, environment, agent.
2. **`HarnessModel.query`** (`harness/model.py`): calls upstream, then normalises `usage` onto
   `message.extra.harness_usage`, emits a `model_call` event, and falls back once if the endpoint
   rejects `seed`/`temperature`/`max_tokens` (recording a degradation).
3. **`HarnessAgent.query`** (`harness/agent.py`): our budget tracker (tokens/steps/calls/wall clock);
   Phase 2 swaps `self.messages` for the compacted view **for the model call only**, then appends the
   response to the canonical timeline — the audit trail is never pruned.
4. **`GuardedEnvironment.execute`** (`harness/environment.py`): sentinel short-circuit → Phase 1 policy
   gate → upstream execution → postprocessors (Phase 3 verification) → secret masking + elision →
   `command` telemetry event. **This is the answer to "between tool call and tool execution".**
5. **`run_end`** — patch capture (`git diff` + synthetic new-file diffs), final verification evidence,
   `REPORT.md` + `status.json`, straggler reaping.

## 4. "What happens between the model returning a tool call and the tool executing?"

1. litellm returns the completion; `LitellmModel._parse_actions` → `parse_toolcall_actions` validates the
   bash tool call and produces `extra.actions = [{command, tool_call_id}]`. A malformed response raises
   `FormatError` *before* anything executes (upstream behaviour, unchanged).
2. `HarnessModel.query` returns the message after usage/telemetry bookkeeping.
3. `HarnessAgent.query` records the step + budget.
4. `DefaultAgent.execute_actions` loops over actions and calls `GuardedEnvironment.execute(action)`.
5. `GuardedEnvironment.execute`:
   a. `harness_*` sentinel? handled in-process, bash never sees it;
   b. call `policy.check(command)` (Phase 1): deterministic rules first, laya second opinion for the
      grey zone; the decision (`allow`/`ask`/`refuse` + probability + reason) is logged;
   c. `refuse` → synthetic observation (`returncode 126`, explanation + safe alternative), the agent
      continues; `allow` → `super().execute()` (upstream, process-group timeouts apply);
   d. postprocessors run (Phase 3: verification output appended as `<verification>`);
   e. output is secret-masked, elided, and logged.
6. `format_toolcall_observation_messages` renders the observation as a `tool` message and
   `DefaultAgent.add_messages` appends it; the model sees it on the next step.

## 5. Prompt composition (persona, Phase 5.2)

`harness/config/build_prompts()` loads upstream `mini.yaml`'s `system_template`/`instance_template`
from the vendored package and **appends** `harness/config/prompts/system_extension.md` and
`instance_extension.md`. The upstream text is never replaced — so upstream improvements remain visible
in a diff, and faculty can point at exactly which lines are ours.

## 6. Compaction: how it decides, and what happens when the judge fails

Pipeline (strict order, tokens are only ever computed by code), implemented in
`harness/compaction/{items,compactor}.py`:

```
messages ──(1) prepare_items ──▶ causal items (assistant call + its tool results)
                                 pins: task/system, credentials, failing-test evidence,
                                       quarantine, newest snapshot per referenced file
                                 recency exemption (12k tokens), duplicate detection
          ──(2) judge_batch ───▶ ONE predict_batch for all candidate items:
                                 {keep|drop|shorten} + P(essential) + staleness score
                                 state = task excerpt + step + bounded item text (<=1100 chars)
          ──(3) fitting ───────▶ drop clear-drops (marker line kept once), shorten long items,
                                 then size-based drops until target_tokens (60k) is met
          ──▶ visible view (never mutates agent.messages)
```

Decision rule per judged item (`harness/compaction/compactor.py::Compactor._judge_items`):

* `drop` requires **P(drop) ≥ 0.60 (calibrated bucket `drop`) AND P(essential) < 0.50 (calibrated
  bucket `essential`) AND staleness ≥ 1.5**;
* otherwise `shorten` if calibrated `P(shorten) ≥ 0.50` and the item is bigger than the minimum;
* otherwise `keep` — borderline items are always kept.

Shadow mode (`mode: shadow`) runs the whole pipeline, logs every verdict, and returns the original
list. Its evidence (the fixture study) is in the README appendix; live is only enabled after it.

**Judge failure modes** (all logged as `degradation` events):

| failure | behaviour |
|---|---|
| laya import/checkpoint/predict failure | `judge_source: heuristic`; only duplicates and size-fitting prune; everything else kept |
| judge returns nothing for an item | item kept (`keep`, reason "judge unavailable per item") |
| judge raises mid-batch | batch kept, degradation event, run continues |

## 7. "What happens between the model returning a tool call and the tool executing?" — with guardrails

1. litellm returns; `LitellmModel._parse_actions` → `parse_toolcall_actions` validates the bash tool
   call and produces `extra.actions = [{command, tool_call_id}]`. A malformed response raises
   `FormatError` *before* anything executes (upstream behaviour, unchanged).
2. `HarnessModel.query` normalises usage, emits a `model_call` event, and may retry once without a
   rejected pinned parameter (recording a degradation).
3. `HarnessAgent.query` checks our budget tracker, obtains the compacted view, calls the model, and
   appends the response to the canonical timeline.
4. `DefaultAgent.execute_actions` calls `GuardedEnvironment.execute(action)`.
5. `GuardedEnvironment.execute` — in order:
   a. `harness_*` sentinel? handled in-process (bash never sees it); `harness_submit_patch` calls
      `_check_finished`, which raises upstream's `Submitted` with the patch summary as the submission;
   b. `ActionPolicy.check(command)`: 12 deterministic rule groups (policy.yaml), workspace-containment
      analysis with real write-target extraction, then — grey zone only — one batched laya call with
      calibrated risk; `allow` / `ask`→refuse (unattended) / `refuse` are all logged with rule,
      probability and temperature;
   c. refuse → synthetic observation (`returncode 126`, reason, calibrated probability, suggested
      alternative); the agent continues;
   d. allow → upstream `super().execute()` (process-group timeout semantics);
   e. postprocessors: `VerificationLoop` appends `<verification status="PASS|FAIL">` when the
      workspace changed; `InjectionScanner` appends `<quarantine>` for instruction-shaped output;
   f. secret masking + size elision + `command` telemetry;
6. `format_toolcall_observation_messages` renders the observation and `add_messages` appends it.

## 7. Failure-mode matrix

| failure | detection | behaviour | where recorded |
|---|---|---|---|
| laya not installed | `ImportError` at first use | heuristic judge (policy: allow grey zone + degradation; compaction: keep-all) | telemetry `degradation`, report "Degradation notes" |
| checkpoint missing/download failed | load timeout / exception | same as above; setup status file kept | `.cache/laya-checkpoint.json`, report |
| judge returns malformed output | missing keys in the answer dict | item kept / command allowed + degradation event | telemetry |
| endpoint rejects pinned `seed` | `BadRequestError` text match | retry once without it, degradation event | telemetry `degradation`, report Reproducibility |
| endpoint lacks price table | litellm cost error | `cost_tracking: ignore_errors`, token budget instead | config, report "not reported" |
| wall/step/token budget hit | our tracker / upstream limits | `LimitsExceeded` graceful exit with summary | telemetry `budget_exhausted`, report status |
| format errors repeat | upstream counter | `RepeatedFormatError` exit, trajectory kept | trajectory, report |
| workspace is not a git repo | `is_git_repo` | no patch artefact / no rollback, note in report | report |
| straggler processes survive | `ps` sweep scoped to the workspace, younger than the run | SIGKILL by process group | telemetry `runaway_reaped` |
| agent issues a refused command | deterministic rule or judge band | synthetic observation, model continues | report Guardrail log |

## 8. Clean-environment verification log (Phase 6.2)

| check | command | result |
|---|---|---|
| unit + mock E2E | `make test` | 110 passed; fixture E2E PASS (submitted / tests pass / report / patch / telemetry) |
| vendored-core purity | `make check-upstream` | `vendored core == upstream v2.4.6 (byte-identical)` |
| fresh copy, degraded path | `scripts/clean_env_check.sh --skip-laya` | `make setup` + `make test` PASS (heuristic judge, no laya) |
| fresh copy, full path | `scripts/clean_env_check.sh` | `make setup` (laya install + checkpoint) + `make test` PASS |
| live endpoint | `make smoke`, `make test-live` | **BLOCKED**: credentials are not exported in the build shell; the user runs this once the evaluator's env vars are present |

The degraded-path check is deliberate: with laya unavailable the whole harness still runs on the
documented heuristics and all tests pass — that is the fallback-first requirement, verified.
