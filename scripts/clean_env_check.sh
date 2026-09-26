#!/usr/bin/env bash
# Clean-environment verification (Phase 6.2).
#
# Copies the repo to a fresh directory (no .venv, no caches, no reports, no
# harness-lab), then runs the evaluator's exact workflow: git init -> make setup
# -> make test. This is the check that "it works from a clean clone".
#
# Usage:
#   scripts/clean_env_check.sh                 # full check (installs laya too)
#   scripts/clean_env_check.sh --skip-laya     # faster: laya steps print SKIPPED
#   scripts/clean_env_check.sh --dest /tmp/x   # choose the destination
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
SKIP_LAYA=0
DEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-laya) SKIP_LAYA=1 ;;
    --dest) shift; DEST="$1" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
DEST="${DEST:-$(mktemp -d "${TMPDIR:-/tmp}/guarded-mini-clean-XXXXXX")}"

echo "== clean-environment check =="
echo "source:      $SRC"
echo "destination: $DEST"
echo "skip laya:   $SKIP_LAYA"

mkdir -p "$DEST"
rsync -a \
  --exclude '.venv' --exclude '.cache' --exclude 'reports/*' --exclude 'harness-lab' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' --exclude '.pytest_cache' \
  "$SRC/" "$DEST/"

cd "$DEST"
git init -q .
git -c user.email=clean@example.invalid -c user.name="clean check" add -A
git -c user.email=clean@example.invalid -c user.name="clean check" commit -qm "clean snapshot"

echo
echo "== make setup =="
SKIP_LAYA="$SKIP_LAYA" make setup

echo
echo "== make test =="
make test

echo
echo "== clean-environment check PASSED =="
echo "artefacts: $DEST/reports/"
