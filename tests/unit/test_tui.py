"""TUI plumbing: telemetry tailing, widgets, git graph, and a headless app smoke."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from harness.tui import HarnessTUI, JsonlTail, git_snapshot, resolve_run_dir
from harness.tui_widgets import context_split_panel, event_line, repo_graph, status_line


# ---------------------------------------------------------------------------
# telemetry tailing
# ---------------------------------------------------------------------------
def test_jsonl_tail_returns_only_new_complete_events(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    path.write_text('{"kind": "run_start"}\n', encoding="utf-8")
    tail = JsonlTail(path)
    assert [e["kind"] for e in tail.poll()] == ["run_start"]
    assert tail.poll() == []  # nothing new

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "model_call"}\n{"kind": "step", "step"')  # second line still partial
    assert [e["kind"] for e in tail.poll()] == ["model_call"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(': 1}\n')
    events = tail.poll()
    assert [e["kind"] for e in events] == ["step"]
    assert events[0]["step"] == 1


def test_resolve_run_dir_accepts_pointer_dir_and_telemetry_file(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    telemetry = run_dir / "telemetry.jsonl"
    telemetry.write_text('{"kind": "run_start"}\n', encoding="utf-8")
    (tmp_path / "LATEST").write_text(str(run_dir) + "\n", encoding="utf-8")

    assert resolve_run_dir(None, tmp_path) == run_dir
    assert resolve_run_dir(tmp_path / "LATEST", tmp_path) == run_dir
    assert resolve_run_dir(run_dir, tmp_path) == run_dir
    assert resolve_run_dir(telemetry, tmp_path) == run_dir
    assert resolve_run_dir(tmp_path / "missing", tmp_path) is None


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------
def _rendered(renderable) -> str:
    console = Console(record=True, width=110, force_terminal=False, legacy_windows=False)
    console.print(renderable)
    return console.export_text()


def test_context_panel_shows_all_sections_and_compaction_savings():
    text = _rendered(
        context_split_panel(
            {"system": 1200, "tools": 300, "messages": 2500, "total": 4000, "limit": 16000, "utilization": 0.25},
            compaction_saved=12345,
            compaction_passes=3,
        )
    )
    for needle in ("system", "tools", "messages", "free", "1,200", "300", "2,500", "4,000/16,000", "25%"):
        assert needle in text, needle
    assert "12,345" in text
    assert "compaction saved" in text


def test_context_panel_before_the_first_model_call():
    text = _rendered(context_split_panel(None, limit=65536))
    assert "0/65,536" in text
    assert "no pruning pass yet" in text


def test_event_line_formats_decision_and_verification_semantics():
    allow = event_line({"kind": "command", "decision": "allow", "command": "ls -la", "returncode": 0, "duration_s": 0.1})
    refuse = event_line({"kind": "command", "decision": "refuse", "command": "rm -rf /", "returncode": 1, "duration_s": 0.1})
    assert "allow" in allow.plain and "ls -la" in allow.plain
    assert "refuse" in refuse.plain

    ok = event_line({"kind": "verification", "ok": True, "command": "pytest -q", "duration_s": 1.0})
    failed = event_line({"kind": "verification", "ok": False, "command": "pytest -q", "duration_s": 1.0})
    assert "PASS" in ok.plain and "FAIL" in failed.plain

    run_end = event_line({"kind": "run_end", "status": "submitted", "stats": {"steps": 4, "total_tokens": 1234, "wall_seconds": 9.5}})
    assert "SUBMITTED" in run_end.plain and "1,234" in run_end.plain


def test_status_line_reports_budget_and_mode():
    text = status_line(
        {"status": "running", "run_id": "r1", "steps": 3, "max_steps": 60, "tokens": 4200, "wall_seconds": 12.5, "mode": "live"}
    ).plain
    for needle in ("running", "r1", "step 3/60", "tok 4,200", "wall 12.5s", "live"):
        assert needle in text, needle


def test_status_line_shows_input_output_and_cache_tokens():
    text = status_line(
        {
            "status": "running",
            "tokens": 12400,
            "tokens_in": 10100,
            "tokens_out": 2300,
            "tokens_cache": 6300,
            "mode": "live",
        }
    ).plain
    for needle in ("tok 12,400", "in 10,100", "cache 6,300", "out 2,300"):
        assert needle in text, needle


# ---------------------------------------------------------------------------
# git graph
# ---------------------------------------------------------------------------
def _git_repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (workspace / "readme.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=workspace, check=True)
    return workspace


def test_git_snapshot_highlights_changed_files(tmp_path):
    workspace = _git_repo(tmp_path)
    (workspace / "pkg" / "mod.py").write_text("value = 2\nnew_line = True\n", encoding="utf-8")
    snapshot = git_snapshot(workspace)
    assert snapshot is not None
    assert snapshot["branch"] in {"main", "master"}
    assert "pkg/mod.py" in snapshot["changed"]
    assert snapshot["added"] >= 2
    tree_text = _rendered(repo_graph("repo", snapshot["files"], snapshot["changed"]))
    assert "mod.py" in tree_text and "pkg/" in tree_text

    assert git_snapshot(tmp_path / "not-a-repo") is None


# ---------------------------------------------------------------------------
# headless app smoke (replay mode — no child process)
# ---------------------------------------------------------------------------
def test_tui_replays_a_finished_run(tmp_path):
    run_dir = tmp_path / "20260101-000000-unit"
    run_dir.mkdir()
    events = [
        {
            "kind": "run_start",
            "iso": "2026-01-01T00:00:00",
            "run_id": run_dir.name,
            "issue": {"title": "fix the failing test", "source": "unit"},
            "workspace": str(tmp_path),
        },
        {
            "kind": "model_call",
            "iso": "2026-01-01T00:00:01",
            "usage": {"total_tokens": 1200},
            "latency_s": 0.4,
            "context_split": {"system": 900, "tools": 200, "messages": 800, "total": 1900, "limit": 16000, "utilization": 0.119, "method": "tiktoken:cl100k_base"},
        },
        {"kind": "compaction", "iso": "2026-01-01T00:00:02", "shadow": False, "tokens_before": 3000, "tokens_after": 1900, "tokens_saved": 1100, "dropped": ["m1", "m2"]},
        {"kind": "run_end", "iso": "2026-01-01T00:00:03", "status": "submitted", "stats": {"steps": 2, "total_tokens": 1200, "wall_seconds": 3.0}},
    ]
    telemetry = run_dir / "telemetry.jsonl"
    telemetry.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    async def smoke() -> None:
        app = HarnessTUI(reports_root=tmp_path, replay=telemetry)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert app._split is not None and app._split["limit"] == 16000
            assert app._saved == 1100 and app._passes == 1
            assert app._state["status"] == "submitted"
            assert len(app.query_one("#log").lines) >= len(events)
            from harness.theme import logo_lines

            assert logo_lines()[0] in str(app.query_one("#brand").render())
            assert not app.query_one("#graph-panel").has_class("hidden")
            app.action_toggle_graph()
            await pilot.pause()
            assert app.query_one("#graph-panel").has_class("hidden")
            assert app.query_one("#graph-panel").display is False
            app.action_toggle_graph()
            await pilot.pause()
            assert app.query_one("#graph-panel").display is not False
            app.action_quit()

    asyncio.run(smoke())


def test_tui_live_mode_quits_and_terminates_the_child(tmp_path):
    import time

    app = HarnessTUI(
        reports_root=tmp_path,
        workspace=tmp_path,
        child_argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
    )

    async def smoke() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(25):
                await pilot.pause(0.2)
                if app._child is not None:
                    break
            assert app._child is not None and app._child.poll() is None
            await pilot.press("q")
            await pilot.pause(0.5)

    asyncio.run(smoke())
    assert app.final_code == 130  # quitting mid-run is an interruption
    for _ in range(30):
        if app._child.poll() is not None:
            break
        time.sleep(0.1)
    assert app._child.poll() is not None, "the harness child must not outlive the TUI"


# ---------------------------------------------------------------------------
# entrypoint launch modes: make run opens the TUI on a terminal, headless when piped
# ---------------------------------------------------------------------------
class _Stream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _args(*argv):
    from harness.entrypoint import build_parser

    return build_parser().parse_args(list(argv))


def test_tui_mode_defaults_to_auto():
    from harness.entrypoint import _tui_mode

    assert _tui_mode(_args()) == "auto"


def test_tui_mode_flags():
    from harness.entrypoint import _tui_mode

    assert _tui_mode(_args("--tui")) == "on"
    assert _tui_mode(_args("--no-tui")) == "off"


def test_tui_mode_rejects_conflicting_flags():
    from harness.entrypoint import _tui_mode

    try:
        _tui_mode(_args("--tui", "--no-tui"))
    except ValueError:
        return
    raise AssertionError("conflicting flags must be rejected")


def test_auto_tui_requires_a_terminal(monkeypatch):
    import harness.entrypoint as ep

    assert ep._should_use_tui("on") is True
    assert ep._should_use_tui("off") is False
    monkeypatch.setattr(ep.sys, "stdin", _Stream(False))
    monkeypatch.setattr(ep.sys, "stdout", _Stream(True))
    assert ep._should_use_tui("auto") is False
    monkeypatch.setattr(ep.sys, "stdin", _Stream(True))
    monkeypatch.setenv("TERM", "xterm-256color")
    assert ep._should_use_tui("auto") is True


# ---------------------------------------------------------------------------
# collect mode: interactive `make run` asks for the issue inside the TUI
# ---------------------------------------------------------------------------
def test_tui_collect_mode_submits_the_issue_and_starts_the_run(tmp_path):
    import time

    from harness.tui import IssueTextArea

    built: list[str] = []

    def collect(text: str) -> list[str]:
        built.append(text)
        return [sys.executable, "-c", "import time; time.sleep(60)"]

    app = HarnessTUI(reports_root=tmp_path, cwd=tmp_path, collect_issue=collect)

    async def smoke() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # the input screen is up, the live view is hidden, no child yet
            assert app.query_one("#issue-view").display is not False
            assert app.query_one("#body").display is False
            assert app._child is None
            # an empty submit only nags; it must not build a run
            await pilot.press("enter")
            await pilot.pause()
            assert "empty" in str(app.query_one("#issue-error").render())
            assert not built
            # a multi-line task submits on Enter, newlines intact
            textarea = app.query_one("#issue-input", IssueTextArea)
            textarea.text = "Fix the off-by-one in buggy.py\nSee tests/test_buggy.py"
            # ctrl+j (some terminals deliver Return as a newline) must also submit
            await pilot.press("ctrl+j")
            for _ in range(30):
                await pilot.pause(0.2)
                if app._child is not None:
                    break
            assert built == ["Fix the off-by-one in buggy.py\nSee tests/test_buggy.py"]
            assert app._child is not None and app._child.poll() is None
            # the input screen is gone, the live view is up
            assert app.query_one("#issue-view").display is False
            assert app.query_one("#body").display is not False
            await pilot.press("q")
            await pilot.pause(0.5)

    asyncio.run(smoke())
    assert app.final_code == 130
    for _ in range(30):
        if app._child.poll() is not None:
            break
        time.sleep(0.1)
    assert app._child.poll() is not None, "the harness child must not outlive the TUI"


def test_tui_collect_mode_shows_collect_errors_and_keeps_the_input_alive(tmp_path):
    from harness.tui import IssueTextArea

    def collect(text: str) -> list[str]:
        raise ValueError("workspace is not a directory: /nope")

    app = HarnessTUI(reports_root=tmp_path, cwd=tmp_path, collect_issue=collect)

    async def smoke() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            textarea = app.query_one("#issue-input", IssueTextArea)
            textarea.text = "Fix the thing"
            await pilot.press("enter")
            for _ in range(30):
                await pilot.pause(0.1)
                if "workspace is not a directory" in str(app.query_one("#issue-error").render()):
                    break
            assert "workspace is not a directory" in str(app.query_one("#issue-error").render())
            assert not app.query_one("#issue-input", IssueTextArea).disabled
            assert app._child is None
            app.action_quit()

    asyncio.run(smoke())


# ---------------------------------------------------------------------------
# collect callback: the missing-key path asks the user to connect, politely
# ---------------------------------------------------------------------------
def test_collect_without_api_key_asks_to_connect(monkeypatch, tmp_path):
    import pytest

    from harness.entrypoint import _make_collect
    from harness.run import UsageError

    monkeypatch.delenv("AI_API_KEY", raising=False)
    collect = _make_collect(_args(), str(tmp_path))
    with pytest.raises(UsageError) as excinfo:
        collect("fix the failing test")
    message = str(excinfo.value)
    assert "Connect the API first" in message
    assert "export AI_API_KEY=" in message
    assert "DeepSeek" in message and "Qwen" in message


def test_collect_with_dry_run_needs_no_key(monkeypatch, tmp_path):
    from harness.entrypoint import _make_collect

    monkeypatch.delenv("AI_API_KEY", raising=False)
    argv = _make_collect(_args("--dry-run", "--workspace", str(tmp_path)), str(tmp_path))("fix the failing test")
    assert argv[0] == sys.executable and "--issue" in argv


def test_collect_with_key_builds_the_child_command(monkeypatch, tmp_path):
    from harness.entrypoint import _make_collect

    monkeypatch.setenv("AI_API_KEY", "sk-test-key-1234567890")
    argv = _make_collect(_args("--workspace", str(tmp_path)), str(tmp_path))("fix the failing test")
    assert argv[0] == sys.executable and "--workspace" in argv


def test_slash_command_palette_runs_commands_after_run_start(tmp_path):
    """'/' opens the palette even after collect mode (focus must not be stranded)."""
    import time

    from harness.tui import IssueTextArea

    def collect(text: str) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(60)"]

    app = HarnessTUI(reports_root=tmp_path, cwd=tmp_path, collect_issue=collect)

    async def smoke() -> None:
        from textual.command import CommandPalette

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#issue-input", IssueTextArea).text = "Fix the thing"
            await pilot.press("enter")
            for _ in range(30):
                await pilot.pause(0.2)
                if app._child is not None:
                    break
            assert app._child is not None
            # the graph is visible; "/" must open the palette (not type into the hidden input)
            assert app.query_one("#graph-panel").display is not False
            await pilot.press("/")
            for _ in range(20):
                await pilot.pause(0.2)
                if isinstance(app.screen, CommandPalette):
                    break
            assert isinstance(app.screen, CommandPalette), "the slash palette must open"
            await pilot.pause(0.6)  # let the search worker deliver hits
            await pilot.press("enter")  # first command: graph
            await pilot.pause(0.3)
            assert app.query_one("#graph-panel").display is False
            await pilot.press("q")
            await pilot.pause(0.5)

    asyncio.run(smoke())
    assert app.final_code == 130
    for _ in range(30):
        if app._child.poll() is not None:
            break
        time.sleep(0.1)
    assert app._child.poll() is not None
