"""
Live TUI — a thin Textual view over the telemetry JSONL stream.

Why it exists: the faculty round wants a face on the harness, and the honest
architecture is a *viewer*: the harness stays headless (the evaluation path),
writes one JSONL line per event, and this app renders that stream live —
context split, agent actions, guardrail decisions, git state. Nothing about the
run depends on the TUI being open; closing it changes nothing.

Two modes, both through `make run`:

* live   — ``make run TUI=1`` launches the normal headless entrypoint as a
           child process and tails the run's telemetry as it is written;
* replay — ``make replay RUN=reports/LATEST`` opens a finished run read-only.

On an interactive terminal, plain ``make run`` opens the TUI in *collect mode*:
an input screen asks for the issue/test text (paste, then Enter) and only then
starts the same headless child run — no raw Ctrl-D stdin prompt.

The graph panel is a Rich tree rebuilt from ``git`` every couple of seconds
(changed files highlighted), toggled with the ⬡ button or ``g``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Footer, Header, RichLog, Static, TextArea

from harness import HARNESS_NAME
from harness.theme import BRAND_BRIGHT, BRAND_DARK, BRAND_WHITE, logo_markup
from harness.tui_widgets import context_split_panel, event_line, repo_graph, status_line

TELEMETRY_NAME = "telemetry.jsonl"
GRAPH_REFRESH_SECONDS = 2.0
STREAM_POLL_SECONDS = 0.4


# ---------------------------------------------------------------------------
# telemetry file plumbing
# ---------------------------------------------------------------------------
class JsonlTail:
    """Incremental reader over an append-only JSONL file.

    Tolerates partial trailing lines (the writer flushes per event, but a read
    can still land mid-line) and never re-reads what it has already returned.
    """

    def __init__(self, path: Path | None = None):
        self.path: Path | None = None
        self._offset = 0
        self._buffer = ""
        if path is not None:
            self.set_path(path)

    def set_path(self, path: Path | None) -> None:
        path = Path(path) if path is not None else None
        if path == self.path:
            return
        self.path = path
        self._offset = 0
        self._buffer = ""

    def poll(self) -> list[dict]:
        if self.path is None or not self.path.exists():
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []
        text = self._buffer + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._buffer = lines.pop() if lines else ""
        events: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


def latest_pointer(reports_root: Path) -> str | None:
    """Contents of reports/LATEST (the newest run directory), if any."""
    pointer = Path(reports_root) / "LATEST"
    try:
        text = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def resolve_run_dir(target: str | Path | None, reports_root: Path) -> Path | None:
    """Accept a run dir, a telemetry.jsonl, reports/LATEST, or nothing."""
    if target is None:
        pointer = latest_pointer(reports_root)
        return Path(pointer) if pointer else None
    path = Path(target).expanduser()
    if path.is_dir():
        return path
    if path.is_file():
        if path.name == "LATEST":
            pointer = path.read_text(encoding="utf-8").strip()
            return Path(pointer) if pointer else None
        if path.name == TELEMETRY_NAME:
            return path.parent
        if (path.parent / TELEMETRY_NAME).exists():
            return path.parent
    return None


# ---------------------------------------------------------------------------
# git snapshot for the graph panel
# ---------------------------------------------------------------------------
def _git(workspace: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def git_snapshot(workspace: Path) -> dict | None:
    """Branch, +/- line stats, changed and tracked files — or None if not a repo."""
    workspace = Path(workspace)
    if not (workspace / ".git").exists() or shutil.which("git") is None:
        return None
    branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip() or "?"
    changed: set[str] = set()
    for line in _git(workspace, "status", "--porcelain=v1", "-uall").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        changed.add(path)
    files = [line for line in _git(workspace, "ls-files", "--cached", "--others", "--exclude-standard").splitlines() if line]
    added = deleted = 0
    for line in _git(workspace, "diff", "--numstat").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added += int(parts[0]) if parts[0].isdigit() else 0
            deleted += int(parts[1]) if parts[1].isdigit() else 0
    return {"branch": branch, "added": added, "deleted": deleted, "changed": changed, "files": files}


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------
class IssueTextArea(TextArea):
    """The collect-mode task box: multi-line paste, Enter submits.

    Enter submits instead of inserting a newline; pasted text arrives as a
    paste (not key presses), so a whole pasted issue keeps its newlines.
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event) -> None:
        # "enter" is the normal submit; "ctrl+j" covers terminals (or pty line
        # disciplines) that deliver the Return key as a literal newline.
        if event.key in ("enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            return
        await super()._on_key(event)


class ContextPanel(Static):
    """The /context-style split panel; fed from model_call/compaction events."""

    def show_split(self, split: dict | None, limit: int, saved: int, passes: int) -> None:
        self.update(
            context_split_panel(split, limit=limit, compaction_saved=saved, compaction_passes=passes)
        )


class GraphPanel(Static):
    """Directory tree with live git stats; refreshed on an interval."""

    def refresh_from(self, workspace: Path | None) -> None:
        if workspace is None:
            self.border_title = "workspace (unknown)"
            self.update(repo_graph("workspace", [], set()))
            return
        snapshot = git_snapshot(workspace)
        if snapshot is None:
            self.border_title = f"{Path(workspace).name} (not a git checkout)"
            self.update(repo_graph(Path(workspace).name, [], set()))
            return
        self.border_title = (
            f"⎇ {snapshot['branch']}   +{snapshot['added']} −{snapshot['deleted']}"
            f"   {len(snapshot['changed'])} changed"
        )
        self.update(repo_graph(Path(workspace).name, snapshot["files"], snapshot["changed"]))


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------
class HarnessTUI(App):
    """Live view over one run's telemetry stream (or a finished run, replay)."""

    TITLE = f"{HARNESS_NAME} · NEUROMANCER"
    SUB_TITLE = "live view over telemetry.jsonl"
    CSS = f"""
    Screen {{ background: $surface; }}
    #titlebar {{ height: 5; }}
    #brand {{ width: 1fr; content-align: left middle; padding: 0 1; }}
    #toggle-graph {{ width: 14; margin: 1 1 0 0; background: {BRAND_DARK}; color: {BRAND_WHITE}; }}
    #body {{ height: 1fr; }}
    #main {{ width: 1fr; }}
    #context-panel {{ height: auto; border: solid {BRAND_BRIGHT}; padding: 0 1; }}
    #log {{ height: 1fr; border: solid {BRAND_DARK}; }}
    #graph-panel {{ width: 44%; border: solid {BRAND_BRIGHT}; padding: 0 1; }}
    #status {{ height: 1; }}
    #issue-view {{ height: 1fr; align: center middle; overflow-y: auto; }}
    #issue-box {{ width: 84; max-width: 90%; height: auto; border: solid {BRAND_BRIGHT}; padding: 1 2; }}
    #issue-brand {{ content-align: center middle; }}
    #issue-prompt {{ margin-top: 1; }}
    #issue-error {{ height: auto; max-height: 5; color: $error; margin-top: 1; }}
    #issue-input {{ height: 6; border: solid {BRAND_DARK}; margin-top: 1; }}
    #issue-hint {{ color: $text-muted; margin-top: 1; }}
    .hidden {{ display: none; }}
    """
    BINDINGS = [
        Binding("g", "toggle_graph", "graph", show=True),
        Binding("f", "toggle_follow", "follow", show=True),
        Binding("q", "quit", "quit", show=True),
        # Priority so it also works while typing in the issue input (a plain
        # "q" is legitimately text there).
        Binding("ctrl+q", "quit", "quit", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        reports_root: Path,
        workspace: Path | None = None,
        child_argv: list[str] | None = None,
        replay: Path | None = None,
        cwd: Path | None = None,
        env: dict | None = None,
        collect_issue: Callable[[str], list[str]] | None = None,
    ):
        super().__init__()
        self.reports_root = Path(reports_root)
        self._workspace = Path(workspace) if workspace else None
        self._child_argv = list(child_argv) if child_argv else None
        self._replay = Path(replay).expanduser() if replay else None
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._env = dict(env) if env is not None else None
        # Collect mode: the TUI asks for the issue itself, then builds the child
        # command through this callback (parse + workspace resolve + staging).
        self._collect_issue = collect_issue

        self._tail = JsonlTail()
        self._child: subprocess.Popen | None = None
        self._latest_before: str | None = None
        self._follow = True

        self._state: dict = {
            "status": "replay" if self._replay else ("awaiting issue" if self._collect_issue else "starting"),
            "run_id": "",
            "steps": None,
            "max_steps": None,
            "tokens": None,
            "tokens_in": None,
            "tokens_out": None,
            "tokens_cache": None,
            "wall_seconds": None,
            "model": "",
            "mode": "replay" if self._replay else "live",
        }
        self._split: dict | None = None
        self._limit = 0
        self._saved = 0
        self._passes = 0
        self.final_code = 0

    # ------------------------------------------------------------- lifecycle
    def compose(self) -> ComposeResult:
        collect = self._collect_issue is not None
        yield Header()
        # The titlebar (brand + graph toggle) only serves the live view; hiding
        # it in collect mode lets the input screen fit an 80x24 terminal.
        with Horizontal(id="titlebar", classes="hidden" if collect else None):
            yield Static(Text.from_markup(logo_markup()), id="brand")
            yield Button("⬡ Graph", id="toggle-graph")
        with Horizontal(id="body", classes="hidden" if collect else None):
            with Vertical(id="main"):
                yield ContextPanel(id="context-panel")
                yield RichLog(id="log", markup=False, wrap=True, max_lines=4000)
            yield GraphPanel(id="graph-panel")
        yield Static(id="status", classes="hidden" if collect else None)
        with Vertical(id="issue-view", classes=None if collect else "hidden"):
            with Vertical(id="issue-box"):
                yield Static(Text.from_markup(logo_markup()), id="issue-brand")
                yield Static("Paste the evaluation issue / test case below, then press Enter.", id="issue-prompt")
                # The note sits above the input so it stays visible even on a
                # small (80x24) terminal, where the box can outgrow the view.
                yield Static("", id="issue-error")
                yield IssueTextArea(id="issue-input")
                yield Static("Enter runs the harness · multi-line paste is fine · Ctrl-Q quits", id="issue-hint")
        yield Footer()

    def on_mount(self) -> None:
        if self._collect_issue is not None:
            self.query_one("#issue-input", IssueTextArea).focus()
            return
        self._begin_viewing()

    def _begin_viewing(self) -> None:
        """Wire the live/replay view and start the refresh timers."""
        log = self.query_one("#log", RichLog)
        log.write(event_line({"kind": "harness", "note": "waiting for telemetry…"}))
        self.query_one(ContextPanel).show_split(None, 0, 0, 0)
        self._refresh_status()

        if self._replay is not None:
            run_dir = resolve_run_dir(self._replay, self.reports_root)
            if run_dir is None:
                self._brand(f"replay target not found: {self._replay}")
                log.write(event_line({"kind": "error", "error": f"replay target not found: {self._replay}"}))
                return
            self._tail.set_path(run_dir / TELEMETRY_NAME)
            self._brand(f"replay · {run_dir}")
            self._poll_events()
            self._refresh_graph()
        elif self._child_argv:
            self._latest_before = latest_pointer(self.reports_root)
            self._brand("starting harness…")
            self._run_child()
        else:
            self._brand("no run command and no replay target")

        self.set_interval(STREAM_POLL_SECONDS, self._poll_events)
        self.set_interval(GRAPH_REFRESH_SECONDS, self._refresh_graph)
        self.set_interval(1.0, self._refresh_status)

    def on_unmount(self) -> None:
        self._terminate_child()

    def _terminate_child(self) -> None:
        if self._child is not None and self._child.poll() is None:
            try:
                self._child.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------- child run
    @work(thread=True, exclusive=True, group="harness-run")
    def _run_child(self) -> None:
        try:
            process = subprocess.Popen(
                self._child_argv,
                cwd=str(self._cwd),
                env=self._env,
                # The child gets its task via --issue; it must not inherit the
                # TUI's terminal stdin. (The harness's own bash runner calls
                # setsid(), which hangs up an inherited pty and kills the TUI's
                # keyboard input — observed on macOS. DEVNULL is also simply
                # correct: the child never reads stdin.)
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001 - the TUI must never crash on launch
            self._post(self._child_failed, f"{type(exc).__name__}: {exc}")
            return
        self._child = process
        self._post(self._set_status, "running")
        if process.stdout is not None:
            for line in process.stdout:
                self._post(self._on_child_line, line.rstrip("\n"))
        code = process.wait()
        self._post(self._on_child_exit, code)

    def _post(self, callback, *args) -> None:
        """Hop from the worker thread to the UI thread; ignore a closed app."""
        try:
            self.call_from_thread(callback, *args)
        except Exception:  # app already exiting; nothing left to update
            pass

    def _child_failed(self, message: str) -> None:
        self.query_one("#log", RichLog).write(event_line({"kind": "error", "error": message}))
        self._brand(f"could not launch the harness: {message}")

    def _on_child_line(self, line: str) -> None:
        line = line.rstrip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "run_id" in payload and "status" in payload:
                self._state.update(
                    status=payload.get("status") or self._state["status"],
                    run_id=payload.get("run_id") or self._state["run_id"],
                )
                patch = payload.get("patch") or ""
                report = payload.get("report") or ""
                self.query_one("#log", RichLog).write(
                    event_line(
                        {
                            "kind": "run_end",
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "status": payload.get("status"),
                            "stats": payload.get("stats") or {},
                        }
                    )
                )
                if patch or report:
                    self.query_one("#log", RichLog).write(
                        event_line({"kind": "report_written", "path": report or patch})
                    )
                return
        if line.strip():
            self.query_one("#log", RichLog).write(event_line({"kind": "harness", "note": line}))

    def _on_child_exit(self, code: int) -> None:
        if self.final_code == 130:
            return  # user quit mid-run: keep the interruption code
        self.final_code = int(code)
        if self._state["status"] in {"starting", "running"}:
            self._state["status"] = "exited" if code else "done"
        self._refresh_status()

    # ------------------------------------------------------------- collect mode
    @on(IssueTextArea.Submitted)
    def _on_issue_submitted(self, event: IssueTextArea.Submitted) -> None:
        text = event.text.strip()
        if not text:
            self._issue_note("the issue text is empty — paste the task first")
            return
        self.query_one("#issue-input", IssueTextArea).disabled = True
        self._issue_note("preparing the run… (resolving the workspace)")
        self._collect_and_run(text)

    @work(thread=True, exclusive=True, group="harness-collect")
    def _collect_and_run(self, text: str) -> None:
        try:
            argv = self._collect_issue(text)
        except Exception as exc:  # noqa: BLE001 - show the usage error, keep the UI alive
            self._post(self._collect_failed, str(exc))
            return
        self._post(self._start_child, argv)

    def _collect_failed(self, message: str) -> None:
        self._issue_note(f"error: {message}")
        self.query_one("#issue-input", IssueTextArea).disabled = False

    def _issue_note(self, text: str) -> None:
        # from_markup: collect errors may carry light styling (bold/dim); plain
        # strings pass through unchanged.
        self.query_one("#issue-error", Static).update(Text.from_markup(text))

    def _start_child(self, argv: list[str]) -> None:
        """The issue is collected and staged: swap the input view for the live view."""
        self._child_argv = argv
        self.query_one("#issue-view").add_class("hidden")
        self.query_one("#titlebar").remove_class("hidden")
        self.query_one("#body").remove_class("hidden")
        self.query_one("#status").remove_class("hidden")
        self._state["status"] = "starting"
        self._begin_viewing()

    # ------------------------------------------------------------- stream
    def _poll_events(self) -> None:
        if self._tail.path is None and self._replay is None:
            pointer = latest_pointer(self.reports_root)
            if pointer and pointer != self._latest_before:
                run_dir = resolve_run_dir(Path(pointer), self.reports_root)
                if run_dir is not None:
                    self._tail.set_path(run_dir / TELEMETRY_NAME)
        for event in self._tail.poll():
            self._absorb(event)

    def _absorb(self, event: dict) -> None:
        kind = event.get("kind")
        self.query_one("#log", RichLog).write(event_line(event))
        if kind == "run_start":
            issue = event.get("issue") or {}
            self._state["run_id"] = event.get("run_id") or self._state["run_id"]
            self._state["status"] = "running"
            if self._workspace is None and event.get("workspace"):
                self._workspace = Path(event["workspace"])
            self._brand(f"{issue.get('title') or issue.get('source') or 'issue'} · {self._workspace or ''}")
        elif kind == "model_call":
            split = event.get("context_split")
            if split:
                self._split = split
                self._limit = int(split.get("limit") or self._limit)
            usage = event.get("usage") or {}
            self._state["tokens"] = (self._state.get("tokens") or 0) + int(usage.get("total_tokens") or 0)
            self._state["tokens_in"] = (self._state.get("tokens_in") or 0) + int(usage.get("prompt_tokens") or 0)
            self._state["tokens_out"] = (self._state.get("tokens_out") or 0) + int(usage.get("completion_tokens") or 0)
            self._state["tokens_cache"] = (self._state.get("tokens_cache") or 0) + int(usage.get("cache_hit_tokens") or 0)
        elif kind == "compaction":
            if not event.get("shadow"):
                self._passes += 1
                self._saved += int(event.get("tokens_saved") or 0)
        elif kind == "step":
            self._state["steps"] = event.get("step")
            budget = event.get("budget") or {}
            self._state["max_steps"] = budget.get("max_steps") or self._state["max_steps"]
            self._state["wall_seconds"] = budget.get("wall_seconds") or self._state["wall_seconds"]
        elif kind == "provider_fallback":
            self._state["model"] = event.get("model") or self._state["model"]
        elif kind == "run_end":
            self._state["status"] = event.get("status") or self._state["status"]
            stats = event.get("stats") or {}
            self._state["wall_seconds"] = stats.get("wall_seconds") or self._state["wall_seconds"]
            self._state["tokens"] = stats.get("total_tokens") or self._state["tokens"]
            self._state["steps"] = stats.get("steps") or self._state["steps"]
        self.query_one(ContextPanel).show_split(self._split, self._limit, self._saved, self._passes)
        self._refresh_status()

    # ------------------------------------------------------------- refresh
    def _refresh_graph(self) -> None:
        self.query_one(GraphPanel).refresh_from(self._workspace)

    def _refresh_status(self) -> None:
        self.query_one("#status", Static).update(status_line(self._state))

    def _brand(self, text: str) -> None:
        """Wordmark + the run/issue line under it (brand palette only)."""
        content = Text.from_markup(logo_markup())
        content.append("\n")
        content.append(f"⬡ {text}", style=BRAND_DARK)
        self.query_one("#brand", Static).update(content)

    def _set_status(self, status: str) -> None:
        self._state["status"] = status
        self._refresh_status()

    # ------------------------------------------------------------- actions
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle-graph":
            self.action_toggle_graph()

    def action_toggle_graph(self) -> None:
        self.query_one("#graph-panel").toggle_class("hidden")

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        log = self.query_one("#log", RichLog)
        log.auto_scroll = self._follow

    def action_quit(self) -> None:
        if self._child is not None and self._child.poll() is None:
            self.final_code = 130
            self._terminate_child()
        self.exit()


def run_tui(
    *,
    reports_root: Path,
    workspace: Path | None = None,
    child_argv: list[str] | None = None,
    replay: Path | None = None,
    cwd: Path | None = None,
    env: dict | None = None,
    collect_issue: Callable[[str], list[str]] | None = None,
) -> int | None:
    """Run the TUI to completion; returns the exit code to propagate.

    ``None`` means the TUI could not start *before* the harness child was
    launched, so the caller may safely fall back to the headless path.
    """
    reports_root = Path(reports_root)
    if replay is not None and resolve_run_dir(replay, reports_root) is None:
        print(f"error: --replay target not found: {replay}", file=sys.stderr)
        return 2
    app = HarnessTUI(
        reports_root=reports_root,
        workspace=workspace,
        child_argv=child_argv,
        replay=replay,
        cwd=cwd,
        env=env,
        collect_issue=collect_issue,
    )
    try:
        app.run()
    except Exception as exc:  # noqa: BLE001 - a TUI failure must not be a traceback
        started = getattr(app, "_child", None) is not None
        print(
            f"error: the TUI could not start ({type(exc).__name__}: {exc}). "
            "Run it from an interactive terminal, or use the headless path (make run).",
            file=sys.stderr,
        )
        return 2 if started else None
    return int(app.final_code or 0)
