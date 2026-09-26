# Example run (reference artefact)

One complete run, captured so reviewers can inspect the report/telemetry/patch format without
running anything. It was produced by the dry-run engine (scripted tool calls, no API calls) against
a copy of `tests/fixture-repo/`; every other layer ran exactly as in a live run:

* a deterministic **refusal** (`cat .env` → credential_access),
* a **laya-judged** grey-zone command with its calibrated probability and temperature,
* **self-verification** evidence (PASS blocks with their trigger),
* the **scope check** and the final **patch**.

Timestamps and temp paths are from the fixture checkout. Generated with:
`harness.entrypoint --dry-run` (scenario in `scripts/`-style tool calls; see NOTES-BUILD.md Phase 1/3).
