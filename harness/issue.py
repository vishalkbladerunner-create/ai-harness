"""
Issue loading and parsing — the evaluator-facing entry contract (Phase 3.2).

Why it exists: the evaluator feeds a GitHub issue or test description as text
(stdin or a file). No network is used: the parser is purely deterministic and
offline, extracting scope hints (file paths, code identifiers, test names) that
the guardrails and the compaction policy consume as "things the task references".

Hackathon criteria served: Phase 3.2 (load_issue), Phase 1.2 (scope hints),
Phase 1.3 (the issue text is data, never instructions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Paths like ``src/foo/bar.py``, ``tests/test_x.py``, ``buggy.py`` (top-level allowed)
PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|rb|go|rs|java|kt|c|cc|cpp|h|hpp|md|toml|yaml|yml|json|cfg|ini|txt))"
)
IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
TEST_RE = re.compile(r"\b(test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)\b")
NUM_RE = re.compile(r"\b(?:line|ln)\s*(\d{1,6})\b", re.IGNORECASE)
#: GitHub repository references (URL or an explicit ``Repo: owner/name`` line).
GITHUB_URL_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[/\s#?]|$)", re.IGNORECASE)
REPO_LINE_RE = re.compile(
    r"(?im)^\s*(?:repo(?:sitory)?|project)\s*:\s*(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?\s*$"
)
#: Absolute filesystem paths mentioned in the issue (URLs are stripped first).
ABS_PATH_RE = re.compile(r"(?<![\w.-])(/(?:[\w.@+-]+/)*[\w.@+-]+)")


@dataclass
class Issue:
    """A parsed evaluation task."""

    text: str
    source: str = "stdin"
    title: str = ""
    paths: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    line_hints: list[int] = field(default_factory=list)
    workspace_hint: str = ""
    repo_url: str = ""
    absolute_paths: list[str] = field(default_factory=list)

    def metadata_markdown(self) -> str:
        lines = [
            f"- source: {self.source}",
            f"- characters: {len(self.text)}",
        ]
        if self.title:
            lines.append(f"- title: {self.title}")
        for label, values in (
            ("referenced paths", self.paths),
            ("referenced identifiers", self.identifiers),
            ("test names", self.tests),
            ("line hints", [str(v) for v in self.line_hints]),
        ):
            if values:
                lines.append(f"- {label}: {', '.join(values[:12])}")
        if self.repo_url:
            lines.append(f"- repository: {self.repo_url}")
        return "\n".join(lines)

    def scope_terms(self) -> list[str]:
        """Terms that a change must be plausibly related to."""
        terms = set()
        for path in self.paths:
            terms.add(path)
            terms.add(Path(path).name)
            terms.add(Path(path).stem)
            parts = Path(path).parts[:-1]
            if parts:
                terms.add("/".join(parts))
        terms.update(self.identifiers)
        terms.update(self.tests)
        return sorted(t for t in terms if t)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "chars": len(self.text),
            "paths": self.paths,
            "identifiers": self.identifiers,
            "tests": self.tests,
            "line_hints": self.line_hints,
            "workspace_hint": self.workspace_hint,
            "repo_url": self.repo_url,
        }


def parse_issue(text: str, source: str = "stdin") -> Issue:
    """Deterministic, offline parse of an issue/test description."""
    text = text.strip("\ufeff \t\r\n")
    issue = Issue(text=text, source=source, title=_first_heading(text))

    for match in PATH_RE.finditer(text):
        candidate = match.group(1).lstrip("./")
        if candidate not in issue.paths:
            issue.paths.append(candidate)
    for match in IDENT_RE.finditer(text):
        name = match.group(1)
        if name.lower() in {"the", "and", "for", "with", "print", "return", "not"}:
            continue
        if name not in issue.identifiers:
            issue.identifiers.append(name)
    for match in TEST_RE.finditer(text):
        if match.group(1) not in issue.tests:
            issue.tests.append(match.group(1))
    for match in NUM_RE.finditer(text):
        value = int(match.group(1))
        if value not in issue.line_hints:
            issue.line_hints.append(value)
    issue.workspace_hint = _workspace_hint(text)
    issue.repo_url = _repo_reference(text)
    issue.absolute_paths = _absolute_paths(text)
    return issue


def load_issue(issue_arg: str | None, stdin_text: str | None = None) -> Issue:
    """Load the issue from a file path, positional path, or stdin text."""
    if issue_arg:
        path = Path(issue_arg).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"--issue path does not exist or is not a file: {issue_arg}")
        return parse_issue(path.read_text(encoding="utf-8", errors="replace"), source=str(path))
    if stdin_text is not None:
        if not stdin_text.strip():
            raise ValueError("no issue text received on stdin (empty input)")
        return parse_issue(stdin_text, source="stdin")
    raise ValueError("no issue supplied: pass --issue <path>, a positional file, or pipe the issue on stdin")


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()[:200]
    first = text.strip().splitlines()
    return (first[0][:200] if first else "")


def _workspace_hint(text: str) -> str:
    """Optional ``Workspace: /path`` / ``Repo: /path`` header in the issue text."""
    match = re.search(r"(?im)^\s*(?:workspace|repo|repository)\s*:\s*(.+?)\s*$", text)
    if not match:
        return ""
    candidate = match.group(1).strip().strip("`'\"")
    if candidate.startswith("/") or candidate.startswith("~") or candidate.startswith("./"):
        return candidate
    return ""


def _repo_reference(text: str) -> str:
    """GitHub repository named by the issue (URL or ``Repo: owner/name`` line)."""
    line = REPO_LINE_RE.search(text)
    if line:
        return f"https://github.com/{line.group(1)}"
    url = GITHUB_URL_RE.search(text)
    if url:
        return f"https://github.com/{url.group(1)}/{url.group(2)}"
    return ""


def _absolute_paths(text: str) -> list[str]:
    """Absolute filesystem paths mentioned in the issue (URLs excluded)."""
    cleaned = re.sub(r"https?://\S+", " ", text)
    found: list[str] = []
    for match in ABS_PATH_RE.finditer(cleaned):
        path = match.group(1).rstrip("/")
        if path and path not in found:
            found.append(path)
    return found
