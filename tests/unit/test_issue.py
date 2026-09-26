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
