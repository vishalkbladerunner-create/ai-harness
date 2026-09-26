#!/usr/bin/env bash
# Create the project venv with a Python >= 3.10, provisioning one if necessary.
#
# Why it exists: macOS ships Python 3.9 at /usr/bin/python3 and the pinned
# upstream core requires >= 3.10. Without this, `make setup` on such a machine
# picks 3.9 and pip fails with "requires a different Python" — an evaluation
# blocker. Resolution order:
#   1. BOOTSTRAP env var (explicit interpreter), if set
#   2. python3.12 / 3.13 / 3.11 / 3.10 on PATH or in the common install dirs
#   3. `python3` if it is >= 3.10, then any other python3.* found
#   4. uv (on PATH, else downloaded into .cache/tools) -> managed CPython 3.12
#   5. a clear, actionable error
#
# Everything provisioned lives under <repo>/.cache (project-local, gitignored).
#
# Usage:
#   scripts/bootstrap_env.sh            # create .venv
#   scripts/bootstrap_env.sh .venv      # create a specific venv directory
#   scripts/bootstrap_env.sh --check    # print the interpreter path only
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
VENV=".venv"
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        *) VENV="$arg" ;;
    esac
done

MIN_VERSION="3.10"
PYTHON_DIRS="${GUARDED_PYTHON_DIRS:-/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin}"

is_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

version_of() {
    "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null
}

find_python() {
    local candidates=() name dir candidate
    if [ -n "${BOOTSTRAP:-}" ]; then
        candidates+=("$BOOTSTRAP")
    fi
    for name in python3.12 python3.13 python3.11 python3.10; do
        if command -v "$name" >/dev/null 2>&1; then
            candidates+=("$(command -v "$name")")
        fi
    done
    IFS=':' read -r -a _dirs <<< "$PYTHON_DIRS"
    for dir in "${_dirs[@]}"; do
        [ -d "$dir" ] || continue
        for name in python3.12 python3.13 python3.11 python3.10; do
            [ -x "$dir/$name" ] && candidates+=("$dir/$name")
        done
        for candidate in "$dir"/python3.*; do
            [ -x "$candidate" ] && candidates+=("$candidate")
        done
    done
    if command -v python3 >/dev/null 2>&1; then
        candidates+=("$(command -v python3)")
    fi
    local seen=""
    for candidate in "${candidates[@]}"; do
        case " $seen " in *" $candidate "*) continue ;; esac
        seen="$seen $candidate"
        if is_supported "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

provision_with_uv() {
    local uv=""
    if command -v uv >/dev/null 2>&1; then
        uv="$(command -v uv)"
    else
        local os arch target url
        os="$(uname -s)"
        arch="$(uname -m)"
        case "$os-$arch" in
            Darwin-arm64) target="aarch64-apple-darwin" ;;
            Darwin-x86_64) target="x86_64-apple-darwin" ;;
            Linux-x86_64) target="x86_64-unknown-linux-gnu" ;;
            Linux-aarch64 | Linux-arm64) target="aarch64-unknown-linux-gnu" ;;
            *)
                echo "  cannot auto-provision Python for platform $os-$arch" >&2
                return 1
                ;;
        esac
        if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
            echo "  curl and tar are required to auto-provision Python" >&2
            return 1
        fi
        mkdir -p .cache/tools
        echo "  no Python >= $MIN_VERSION found; downloading uv ($target) into .cache/tools ..." >&2
        url="https://github.com/astral-sh/uv/releases/latest/download/uv-$target.tar.gz"
        if ! curl -fsSL "$url" | tar -xz -C .cache/tools --strip-components=1 "uv-$target/uv"; then
            echo "  could not download uv" >&2
            return 1
        fi
        chmod +x .cache/tools/uv
        uv="$REPO_ROOT/.cache/tools/uv"
    fi
    export UV_CACHE_DIR="$REPO_ROOT/.cache/uv"
    export UV_PYTHON_INSTALL_DIR="$REPO_ROOT/.cache/uv-python"
    echo "  provisioning CPython 3.12 with uv (project-local cache) ..." >&2
    if ! "$uv" python install --no-bin 3.12 >&2; then
        echo "  uv could not install CPython 3.12" >&2
        return 1
    fi
    local found
    found="$("$uv" python find --managed-python --no-project 3.12 2>/dev/null || true)"
    if [ -z "$found" ]; then
        found="$("$uv" python find --managed-python 3.12 2>/dev/null || true)"
    fi
    if [ -z "$found" ]; then
        found="$("$uv" python find 3.12 2>/dev/null || true)"
    fi
    if [ -n "$found" ] && is_supported "$found"; then
        echo "$found"
        return 0
    fi
    echo "  uv did not produce a usable interpreter" >&2
    return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    PYTHON="$(provision_with_uv || true)"
fi
if [ -z "$PYTHON" ]; then
    cat >&2 <<EOF
ERROR: guarded-mini requires Python >= $MIN_VERSION and none could be found or provisioned.

Install a newer Python and re-run 'make setup', for example:
    macOS (Homebrew):  brew install python@3.12
    Debian/Ubuntu:     sudo apt install python3.12 python3.12-venv
    Fedora:            sudo dnf install python3.12
or install uv (https://docs.astral.sh/uv/) which can provision one automatically:
    curl -LsSf https://astral.sh/uv/install.sh | sh
EOF
    exit 1
fi

if [ "$CHECK_ONLY" = "1" ]; then
    echo "$PYTHON"
    exit 0
fi

echo "== bootstrap interpreter: $PYTHON ($(version_of "$PYTHON")) =="
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
echo "== venv created: $VENV/bin/python ($(version_of "$VENV/bin/python")) =="
