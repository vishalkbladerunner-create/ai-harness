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
#: A GitHub issue or pull-request link (``…/issues/123`` or ``…/pull/123``).
ISSUE_URL_RE = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+?)/(?:issues|pull)/(\d+)(?=[/\s#?]|$)", re.IGNORECASE
)
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


def issue_link(text: str) -> tuple[str, str] | None:
    """First GitHub issue/PR link in the text as (api_url, canonical_url), or None."""
    match = ISSUE_URL_RE.search(text or "")
    if not match:
        return None
    owner, repo, number = match.group(1), match.group(2), match.group(3)
    return f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", match.group(0)


def fetch_issue_text(text: str, *, timeout: int = 15) -> str | None:
    """Fetch title+body for a pasted GitHub issue/PR link.

    Primary source is GitHub's public API (60 unauthenticated requests/hour per
    IP); when that is exhausted the fallback is the public HTML page (title +
    description snippet — not rate-limited the same way). Returns the task text
    (title + body + the original link, which keeps the repo reference for
    workspace cloning), or None on any failure — callers decide whether to fall
    back or guide the user. Parsing itself stays offline; this is the single,
    explicit network step for pasted links.
    """
    import json
    import urllib.request

    reference = issue_link(text)
    if reference is None:
        return None
    api_url, canonical = reference

    def _open(url: str, accept: str) -> bytes | None:
        request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "guarded-mini-harness"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception:
            return None

    raw = _open(api_url, "application/vnd.github+json")
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            title = str(data.get("title") or "").strip()
            body = str(data.get("body") or "").strip()
            if title or body:
                return f"# {title}\n\n{body}\n\n---\nOriginal link: {text.strip()}"

    # Fallback: the public HTML page (not subject to the API rate limit).
    import html as html_module

    page = _open(canonical, "text/html,application/xhtml+xml")
    if page is None:
        return None
    page_text = page.decode("utf-8", errors="replace")
    title_match = re.search(r"<title>(.*?)</title>", page_text, re.DOTALL)
    desc_match = re.search(r'<meta\s+name="description"\s+content="(.*?)"', page_text, re.DOTALL)
    title = html_module.unescape(title_match.group(1)).strip() if title_match else ""
    title = re.sub(r"\s*·\s*(Issue|Pull Request) #\d+.*$", "", title).strip()
    body = html_module.unescape(desc_match.group(1)).strip() if desc_match else ""
    if not title and not body:
        return None
    note = "(from the page snippet — paste the full issue text for best results)"
    return f"# {title}\n\n{body}\n\n{note}\n\n---\nOriginal link: {text.strip()}"


def _absolute_paths(text: str) -> list[str]:
    """Absolute filesystem paths mentioned in the issue (URLs excluded)."""
    cleaned = re.sub(r"https?://\S+", " ", text)
    found: list[str] = []
    for match in ABS_PATH_RE.finditer(cleaned):
        path = match.group(1).rstrip("/")
        if path and path not in found:
            found.append(path)
    return found
