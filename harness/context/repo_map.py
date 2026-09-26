"""
Repository map — compact file tree + symbol map injected at session start.

Why it exists: Phase 3.3 requires a bounded repo map so the model does not have to
spend its first ten steps discovering the layout. Bounded by construction: the
map has a character budget and stops adding symbols when it is exhausted, so it
can never blow up the context window.

Hackathon criteria served: Phase 3.3 (repo-map injection), Phase 1.4 (context is
a resource and is budgeted like one).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".cache",
    "harness/checkpoints", ".tox", ".idea", ".vscode", "target", "coverage",
}
IGNORED_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".so", ".dylib", ".dll", ".exe", ".bin", ".lock", ".min.js", ".map",
    ".pyc", ".class", ".jar", ".mp4", ".mov", ".woff", ".woff2", ".ttf",
}
_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _iter_files(root: Path, max_files: int):
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= max_files:
            return
        rel_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS for part in rel_parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in IGNORED_SUFFIXES or path.name.startswith("."):
            continue
        try:
            if path.stat().st_size > 200_000:
                continue
        except OSError:
            continue
        count += 1
        yield path


def _python_symbols(path: Path, limit: int = 40) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError, ValueError):
        return []
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}()")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            inner = f" [{', '.join(methods[:6])}{', ...' if len(methods) > 6 else ''}]" if methods else ""
            symbols.append(f"class {node.name}{inner}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append(f"{target.id} = ...")
        if len(symbols) >= limit:
            break
    return symbols


def _regex_symbols(path: Path, limit: int = 25) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(dict.fromkeys(_SYMBOL_RE.findall(text)))[:limit]


def build_repo_map(root: str | Path, max_chars: int = 6000, max_files: int = 400) -> str:
    """Return a bounded textual map of the repository at *root*."""
    root = Path(root).resolve()
    if not root.exists():
        return f"(workspace {root} does not exist)"
    lines: list[str] = []
    used = 0

    def add(line: str) -> bool:
        nonlocal used
        if used + len(line) + 1 > max_chars:
            return False
        lines.append(line)
        used += len(line) + 1
        return True

    add(f"root: {root}")
    add("")
    add("## tree")
    truncated = False
    for path in _iter_files(root, max_files):
        rel = path.relative_to(root)
        if not add(f"  {rel}"):
            truncated = True
            break
    if truncated:
        add("  ... (truncated)")

    add("")
    add("## symbols")
    symbols_shown = 0
    for path in _iter_files(root, max_files):
        if path.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs"}:
            continue
        if path.suffix.lower() == ".py":
            symbols = _python_symbols(path)
        else:
            symbols = _regex_symbols(path)
        if not symbols:
            continue
        header = f"  {path.relative_to(root)}:"
        if not add(header):
            break
        for symbol in symbols:
            if not add(f"    - {symbol}"):
                truncated = True
                break
        symbols_shown += 1
        if truncated:
            break
    if not symbols_shown:
        add("  (no symbols extracted)")
    elif truncated:
        add("  ... (truncated)")
    return "\n".join(lines)
