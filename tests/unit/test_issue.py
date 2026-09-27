"""Deterministic, offline issue parsing (Phase 3.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.issue import load_issue, parse_issue

SAMPLE = """# Fix last_item off-by-one

test_buggy.py fails. In buggy.py the function last_item() at line 12 returns items[len(items)].

Workspace: /tmp/somewhere

See tests for details.
"""


def test_parses_paths_identifiers_tests_and_line_hints():
    issue = parse_issue(SAMPLE, source="unit")
    assert issue.title == "Fix last_item off-by-one"
    assert "buggy.py" in issue.paths
    assert "test_buggy.py" in issue.paths
    assert "last_item" in issue.identifiers
    assert "test_buggy" in issue.tests
    assert 12 in issue.line_hints
    assert issue.workspace_hint == "/tmp/somewhere"


def test_scope_terms_include_paths_and_symbols():
    terms = parse_issue(SAMPLE).scope_terms()
    assert "buggy.py" in terms
    assert "last_item" in terms


def test_load_issue_from_file(tmp_path: Path):
    path = tmp_path / "issue.md"
    path.write_text(SAMPLE, encoding="utf-8")
    issue = load_issue(str(path))
    assert issue.source == str(path)
    assert "last_item" in issue.identifiers


def test_load_issue_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_issue(str(tmp_path / "nope.md"))


def test_load_issue_from_stdin_text():
    issue = load_issue(None, stdin_text="please fix `src/app.py`")
    assert issue.source == "stdin"
    assert "src/app.py" in issue.paths


def test_load_issue_requires_input():
    with pytest.raises(ValueError):
        load_issue(None, stdin_text="   ")


def test_parses_repo_reference_and_absolute_paths():
    issue = parse_issue("Repo: acme/widgets\nFix /tmp/target-repo/app.py\n", source="unit")
    assert issue.repo_url == "https://github.com/acme/widgets"
    assert "/tmp/target-repo/app.py" in issue.absolute_paths


def test_repo_url_from_issue_link_and_url_paths_ignored():
    issue = parse_issue("See https://github.com/acme/widgets/issues/42 for details", source="unit")
    assert issue.repo_url == "https://github.com/acme/widgets"
    # path segments of the URL must not be mistaken for filesystem paths
    assert not any(path.startswith("/acme") for path in issue.absolute_paths)


def test_repo_url_is_not_a_workspace_hint():
    issue = parse_issue("Repo: https://github.com/acme/widgets\n", source="unit")
    assert issue.repo_url == "https://github.com/acme/widgets"
    assert issue.workspace_hint == ""


# ---------------------------------------------------------------------------
# pasted GitHub issue/PR links: parse, fetch (mocked), fallback, guidance
# ---------------------------------------------------------------------------
def test_issue_link_parses_issues_and_pull_urls():
    from harness.issue import issue_link

    api, canonical = issue_link("see https://github.com/psf/requests/issues/1921 for details")
    assert api == "https://api.github.com/repos/psf/requests/issues/1921"
    assert canonical == "https://github.com/psf/requests/issues/1921"
    api, _ = issue_link("https://github.com/sympy/sympy/pull/22914")
    assert api == "https://api.github.com/repos/sympy/sympy/issues/22914"
    assert issue_link("fix the failing test in buggy.py") is None
    assert issue_link("https://github.com/owner/repo") is None  # repo link, not an issue link


class _FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_issue_text_uses_the_api_when_it_works(monkeypatch):
    import json as json_module

    from harness.issue import fetch_issue_text

    payload = json_module.dumps({"title": "Bug in frob", "body": "the frob is broken"}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(payload))
    text = fetch_issue_text("https://github.com/o/r/issues/7")
    assert "# Bug in frob" in text and "the frob is broken" in text
    assert "Original link: https://github.com/o/r/issues/7" in text


def test_fetch_issue_text_falls_back_to_the_html_page(monkeypatch):
    from harness.issue import fetch_issue_text

    calls = []

    def fake_open(request, timeout=0):
        calls.append(request.full_url)
        if "api.github.com" in request.full_url:
            raise OSError("403 rate limited")
        page = (
            "<html><head><title>Fix the thing · Issue #7 · o/r · GitHub</title>"
            '<meta name="description" content="the thing is broken and must be fixed">'
            "</head></html>"
        )
        return _FakeResponse(page.encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    text = fetch_issue_text("https://github.com/o/r/issues/7")
    assert len(calls) == 2, "must try the API first, then the HTML page"
    assert "# Fix the thing" in text
    assert "the thing is broken and must be fixed" in text
    assert "page snippet" in text


def test_fetch_issue_text_returns_none_when_both_sources_fail(monkeypatch):
    from harness.issue import fetch_issue_text

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert fetch_issue_text("https://github.com/o/r/issues/7") is None


def test_resolve_issue_text_guides_on_a_bare_unfetchable_link(monkeypatch):
    import pytest

    from harness.entrypoint import _resolve_issue_text
    from harness.issue import parse_issue
    from harness.run import GuidanceError

    monkeypatch.setattr("harness.entrypoint.fetch_issue_text", lambda text, **k: None)
    with pytest.raises(GuidanceError) as excinfo:
        _resolve_issue_text(parse_issue("https://github.com/o/r/issues/7"))
    assert "Paste the issue text" in str(excinfo.value)


def test_resolve_issue_text_reparses_a_fetched_issue(monkeypatch):
    from harness.entrypoint import _resolve_issue_text
    from harness.issue import parse_issue

    monkeypatch.setattr(
        "harness.entrypoint.fetch_issue_text",
        lambda text, **k: "# Real title\n\nreal body\n\n---\nOriginal link: https://github.com/o/r/issues/7",
    )
    issue = _resolve_issue_text(parse_issue("https://github.com/o/r/issues/7"))
    assert issue.title == "Real title"
    assert "real body" in issue.text
    assert issue.repo_url == "https://github.com/o/r"
