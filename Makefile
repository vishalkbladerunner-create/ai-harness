# ============================================================================
# Guarded-mini harness — evaluation-facing Makefile
#
# Evaluator workflow (must work unmodified):
#   git clone <this repo> && cd <repo>
#   export AI_API_KEY=...        # the only variable the committee exports
#   make setup
#   make run            # interactive: TUI asks for the task (paste + Enter);
#                       # scripted: the task arrives on stdin (or ISSUE=path)
#
# MODEL_BASE_URL / MODEL_NAME are optional: checked-in provider defaults
# (DeepSeek then Qwen) are used when they are absent.
#
# Design rules honoured here:
#   * No secret material in this file. Credentials are only ever read from the
#     caller's environment and passed through to the entrypoint at runtime.
#   * Everything project-local: the venv lives in .venv/, caches in .cache/,
#     reports in reports/.
#   * laya is an *optional* sidecar. If it cannot be installed or its
#     checkpoint cannot be downloaded, setup notes the degradation and the
#     harness keeps working with documented heuristics.
# ============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV        := .venv
# BOOTSTRAP: optional explicit interpreter (e.g. make setup BOOTSTRAP=python3.12).
# Empty means "auto": scripts/bootstrap_env.sh finds a Python >= 3.10, or
# provisions a managed CPython 3.12 via uv when the machine only has an older one.
BOOTSTRAP   ?=
PY          := $(VENV)/bin/python
PIP         := $(PY) -m pip

CONSTRAINTS ?= constraints.txt

# Entrypoint knobs (all optional; see README "Running the harness")
ISSUE       ?=
WORKSPACE   ?=
ARGS        ?=
BUDGET      ?=
TUI         ?=
RUN         ?=
# TUI=1 -> force the live terminal UI, TUI=0 -> force headless. Default: the
# entrypoint opens the TUI on an interactive terminal and stays headless when
# the issue is piped (the committee path).
TUI_ARGS    := $(if $(TUI),$(if $(filter 0 off no false,$(TUI)),--no-tui,--tui),)

# ---------------------------------------------------------------------------
help:
	@echo "guarded-mini — targets"
	@echo ""
	@echo "  make setup     create .venv, install pinned mini-swe-agent (vendored) + deps,"
	@echo "                 install laya + checkpoint (both NON-FATAL, cached)"
	@echo "                 (finds Python >= 3.10; provisions a managed CPython via uv if absent)"
	@echo "  make run       run our entrypoint against a task (stdin or ISSUE=path;"
	@echo "                 interactive terminals get a TUI input field for the issue)"
	@echo "                 (only AI_API_KEY is required; MODEL_BASE_URL/MODEL_NAME optional)"
	@echo "  make run TUI=1 same run, live terminal UI (graph + context panels; q quits)"
	@echo "                 (interactive terminals open the TUI by default; TUI=0 forces headless)"
	@echo "  make replay    open a finished run in the TUI (RUN=reports/LATEST default)"
	@echo "  make test      unit tests + dry-run E2E on the fixture repo (no API calls;"
	@echo "                 E2E_LIVE=1 adds one live pass on your own credential)"
	@echo "  make test-live force the live eval against tests/fixture-repo (needs creds)"
	@echo "  make smoke     one trivial live task: create hello.txt containing done"
	@echo "  make doctor    probe DeepSeek + Qwen with your AI_API_KEY (auth + model IDs)"
	@echo "  make bench     run + score SWE-bench Verified instances end to end"
	@echo "                 (INSTANCE=\"sympy__sympy-22914 …\"; needs AI_API_KEY)"
	@echo "  make clean     remove .venv, caches, generated reports"
	@echo ""
	@echo "  make run ISSUE=path/to/issue.md WORKSPACE=/path/to/target-repo"
	@echo "  make run < issue.md"
	@echo "  make run ARGS=\"--verify on --budget 0.50\" BUDGET=0.50"
	@echo "  make run TUI=1 ARGS=\"--dry-run\" < issue.md   # TUI demo without credentials"

# ---------------------------------------------------------------------------
# Every entry point depends on venv-guard: if .venv is missing or was built with
# an unsupported Python (macOS system Python is 3.9), it is rebuilt with a
# >= 3.10 interpreter (provisioned via uv if the machine has none) and the core
# dependencies are installed. A healthy venv is a no-op (~50 ms).
.PHONY: venv-guard
venv-guard:
	@if [ -x "$(PY)" ] && $(PY) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then \
		:; \
	else \
		echo "== creating a usable venv (Python >= 3.10) =="; \
		rm -rf $(VENV); \
		BOOTSTRAP="$(BOOTSTRAP)" bash scripts/bootstrap_env.sh $(VENV); \
		$(PIP) install --quiet --upgrade pip wheel setuptools; \
		echo "== installing pinned mini-swe-agent (vendored, v2.4.6) =="; \
		if [ -f "$(CONSTRAINTS)" ]; then \
			$(PIP) install --quiet -c "$(CONSTRAINTS)" -e vendor/mini-swe-agent || \
			$(PIP) install --quiet -e vendor/mini-swe-agent; \
		else \
			$(PIP) install --quiet -e vendor/mini-swe-agent; \
		fi; \
		$(PIP) install --quiet -e . --no-deps; \
		$(PIP) install --quiet pytest; \
	fi

setup: venv-guard
	@echo "== installing pinned mini-swe-agent (vendored, v2.4.6) =="
	@if [ -f "$(CONSTRAINTS)" ]; then \
		$(PIP) install --quiet -c "$(CONSTRAINTS)" -e vendor/mini-swe-agent || { \
			echo "   constrained install failed; retrying unpinned (setup must not fail on a pin)"; \
			$(PIP) install --quiet -e vendor/mini-swe-agent; }; \
	else \
		$(PIP) install --quiet -e vendor/mini-swe-agent; \
	fi
	@echo "== installing harness package (editable, no deps) =="
	$(PIP) install --quiet -e . --no-deps
	@echo "== installing harness test deps =="
	@if [ -f "$(CONSTRAINTS)" ]; then \
		$(PIP) install --quiet -c "$(CONSTRAINTS)" pytest || $(PIP) install --quiet pytest; \
	else \
		$(PIP) install --quiet pytest; \
	fi
	@echo "== installing TUI deps (optional; failure is non-fatal) =="
	@if [ -f "$(CONSTRAINTS)" ]; then \
		$(PIP) install --quiet -c "$(CONSTRAINTS)" "textual>=1" tiktoken || \
		$(PIP) install --quiet "textual>=1" tiktoken || \
		echo "   TUI deps unavailable — headless path unaffected (make run TUI=1 will explain)"; \
	else \
		$(PIP) install --quiet "textual>=1" tiktoken || \
		echo "   TUI deps unavailable — headless path unaffected (make run TUI=1 will explain)"; \
	fi
	@if [ "$${SKIP_LAYA:-0}" = "1" ]; then \
		echo "== laya sidecar: SKIPPED (SKIP_LAYA=1) — harness will run in degraded mode =="; \
	else \
		echo "== installing laya (optional sidecar; failure is non-fatal) =="; \
		$(PY) scripts/setup_laya.py || true; \
		echo "== fetching laya checkpoint (optional; cached; failure is non-fatal) =="; \
		$(PY) scripts/fetch_checkpoint.py || true; \
	fi
	@echo ""
	@echo "setup complete. Run 'make run' with AI_API_KEY / MODEL_BASE_URL / MODEL_NAME exported."

run: venv-guard
	@AI_API_KEY=$${AI_API_KEY} \
	 MODEL_BASE_URL=$${MODEL_BASE_URL} \
	 MODEL_NAME=$${MODEL_NAME} \
	 $(PY) -m harness.entrypoint \
	 $(TUI_ARGS) \
	 $(if $(WORKSPACE),--workspace "$(WORKSPACE)",) \
	 $(if $(ISSUE),--issue "$(ISSUE)",) \
	 $(if $(BUDGET),--budget $(BUDGET),) $(ARGS)

# Open a finished run in the TUI (no credentials, no model calls).
replay: venv-guard
	@$(PY) -m harness.entrypoint --tui --replay "$(if $(RUN),$(RUN),reports/LATEST)"

test: venv-guard
	@$(MAKE) --no-print-directory test-unit
	@$(MAKE) --no-print-directory test-e2e

test-unit: venv-guard
	@echo "== unit tests (no model calls) =="
	$(PY) -m pytest tests/unit -q

test-e2e: venv-guard
	@echo "== dry-run E2E on tests/fixture-repo (no API calls; local mock endpoint) =="
	$(PY) scripts/e2e_fixture.py --mode mock
	@if [ "$${E2E_LIVE:-0}" = "1" ]; then \
		if [ -n "$$AI_API_KEY" ]; then \
			echo "== live E2E on tests/fixture-repo (E2E_LIVE=1; spends the exported credential) =="; \
			$(PY) scripts/e2e_fixture.py --mode live || exit 1; \
		else \
			echo "== live E2E REQUESTED but SKIPPED (AI_API_KEY not exported) =="; \
		fi \
	else \
		echo "== live E2E SKIPPED (opt in with: E2E_LIVE=1 make test, or make test-live) =="; \
		echo "   the evaluation run never spends the evaluator's credential from make test"; \
	fi

test-live: venv-guard
	$(PY) scripts/e2e_fixture.py --mode live

smoke: venv-guard
	$(PY) scripts/smoke.py

# Pre-flight: which official provider accepts the exported AI_API_KEY (no run).
doctor: venv-guard
	@$(PY) scripts/check_providers.py

# Real benchmark: run + score SWE-bench Verified instances end to end.
#   make bench INSTANCE="sympy__sympy-22914 sympy__sympy-23950"
bench: venv-guard
	@$(PY) scripts/swebench.py $(INSTANCE)

# Optional: prove the vendored core is byte-identical to the upstream tag.
check-upstream:
	@tmp=$$(mktemp -d) && git clone --quiet https://github.com/SWE-agent/mini-swe-agent "$$tmp/up" && \
	 git -C "$$tmp/up" checkout --quiet v2.4.6 && \
	 diff -r --exclude=.git --exclude=.github --exclude=PINNED --exclude=__pycache__ --exclude='*.egg-info' \
	   "$$tmp/up" vendor/mini-swe-agent && echo "vendored core == upstream v2.4.6 (byte-identical)" && \
	 rm -rf "$$tmp"

# Phase 6.2: fresh-copy check of the evaluator workflow (setup + test).
check-clean-env:
	bash scripts/clean_env_check.sh

clean:
	@echo "== cleaning build artefacts, caches and generated reports =="
	rm -rf $(VENV)
	rm -rf .pytest_cache
	rm -rf .cache
	rm -rf harness/checkpoints
	find . -path ./vendor -prune -o -name '__pycache__' -type d -print -exec rm -rf {} + 2>/dev/null || true
	find . -path ./vendor -prune -o -name '*.pyc' -type f -print -delete 2>/dev/null || true
	find reports -mindepth 1 -maxdepth 1 ! -name '.gitkeep' ! -name 'EXAMPLE' -print -exec rm -rf {} + 2>/dev/null || true
	@echo "clean complete (reports/EXAMPLE is kept as the reference artefact)."

.PHONY: help setup run replay test test-unit test-e2e test-live smoke doctor bench clean check-upstream check-clean-env
