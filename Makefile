# ============================================================================
# Guarded-mini harness — evaluation-facing Makefile
#
# Evaluator workflow (must work unmodified):
#   git clone <this repo> && cd <repo>
#   export AI_API_KEY ...; export MODEL_BASE_URL ...; export MODEL_NAME ...
#   make setup
#   make run            # task arrives on stdin (or ISSUE=path)
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
BOOTSTRAP   ?= $(shell for p in python3.13 python3.12 python3.11 python3.10 python3; do command -v $$p >/dev/null 2>&1 && { echo $$p; break; }; done)
PY          := $(VENV)/bin/python
PIP         := $(PY) -m pip
VENV_STAMP  := $(VENV)/.ready

# Entrypoint knobs (all optional; see README "Running the harness")
ISSUE       ?=
WORKSPACE   ?=
ARGS        ?=
BUDGET      ?=

# ---------------------------------------------------------------------------
help:
	@echo "guarded-mini — targets"
	@echo ""
	@echo "  make setup     create .venv, install pinned mini-swe-agent (vendored) + deps,"
	@echo "                 install laya + checkpoint (both NON-FATAL, cached)"
	@echo "  make run       run our entrypoint against a task (stdin or ISSUE=path)"
	@echo "  make test      unit tests + dry-run E2E on the fixture repo (no API calls),"
	@echo "                 plus live E2E when creds are present"
	@echo "  make test-live force the live eval against tests/fixture-repo (needs creds)"
	@echo "  make smoke     one trivial live task: create hello.txt containing done"
	@echo "  make clean     remove .venv, caches, generated reports"
	@echo ""
	@echo "  make run ISSUE=path/to/issue.md WORKSPACE=/path/to/target-repo"
	@echo "  make run < issue.md"
	@echo "  make run ARGS=\"--verify on --budget 0.50\" BUDGET=0.50"

# ---------------------------------------------------------------------------
$(VENV_STAMP):
	@echo "== bootstrap interpreter: $(BOOTSTRAP) =="
	$(BOOTSTRAP) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip wheel setuptools
	@touch $@

setup: $(VENV_STAMP)
	@echo "== installing pinned mini-swe-agent (vendored, v2.4.6) =="
	$(PIP) install --quiet -e vendor/mini-swe-agent
	@echo "== installing harness package (editable, no deps) =="
	$(PIP) install --quiet -e . --no-deps
	@echo "== installing harness test deps =="
	$(PIP) install --quiet pytest
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

run: $(VENV_STAMP)
	@AI_API_KEY=$${AI_API_KEY} \
	 MODEL_BASE_URL=$${MODEL_BASE_URL} \
	 MODEL_NAME=$${MODEL_NAME} \
	 $(PY) -m harness.entrypoint \
	 $(if $(WORKSPACE),--workspace "$(WORKSPACE)",) \
	 $(if $(ISSUE),--issue "$(ISSUE)",) \
	 $(if $(BUDGET),--budget $(BUDGET),) $(ARGS)

test: $(VENV_STAMP)
	@$(MAKE) --no-print-directory test-unit
	@$(MAKE) --no-print-directory test-e2e

test-unit: $(VENV_STAMP)
	@echo "== unit tests (no model calls) =="
	$(PY) -m pytest tests/unit -q

test-e2e: $(VENV_STAMP)
	@echo "== dry-run E2E on tests/fixture-repo (no API calls; local mock endpoint) =="
	$(PY) scripts/e2e_fixture.py --mode mock
	@if [ -n "$$AI_API_KEY" ] && [ -n "$$MODEL_BASE_URL" ] && [ -n "$$MODEL_NAME" ]; then \
		echo "== live E2E on tests/fixture-repo (creds present) =="; \
		$(PY) scripts/e2e_fixture.py --mode live || exit 1; \
	else \
		echo "== live E2E SKIPPED (AI_API_KEY / MODEL_BASE_URL / MODEL_NAME not exported) =="; \
	fi

test-live: $(VENV_STAMP)
	$(PY) scripts/e2e_fixture.py --mode live

smoke: $(VENV_STAMP)
	$(PY) scripts/smoke.py

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

.PHONY: help setup run test test-unit test-e2e test-live smoke clean check-upstream check-clean-env
