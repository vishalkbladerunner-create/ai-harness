"""
Dry-run model — scripted, offline tool-call sequences for API-free testing.

Why it exists: Phase 6 requires a dry-run mode that needs no API calls. It uses
upstream's own ``DeterministicToolcallModel`` (the class mini-swe-agent uses in
its tests) so the dry run exercises the same parsing/observation code paths as a
live run, minus the network.

Hackathon criteria served: Phase 6.1 (dry-run mode), Phase 0.4 (E2E proving).
"""

from __future__ import annotations

import json
import time

from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output

DEFAULT_OBSERVATION_TEMPLATE = (
    "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
    "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
)


def toolcall(command: str, thought: str = "", cost: float = 0.0, call_id: str | None = None) -> dict:
    call_id = call_id or f"call_{abs(hash(command)) % 10**8:08d}"
    arguments = json.dumps({"command": command})
    return make_toolcall_output(
        thought or "dry-run step",
        [{"id": call_id, "type": "function", "function": {"name": "bash", "arguments": arguments}}],
        [{"command": command, "tool_call_id": call_id}],
    )


def default_scenario() -> list[dict]:
    """Prove the plumbing: inspect, create a marker, run the toolchain, submit."""
    return [
        toolcall("pwd && ls -a", "Look at the workspace before doing anything."),
        toolcall("printf 'done\\n' > harness_dry_run_marker.txt && cat harness_dry_run_marker.txt", "Write the marker file."),
        toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "Everything checks out; submit."),
    ]


def fixture_scenario() -> list[dict]:
    """Repair the off-by-one in the fixture repo, mirroring a live fix."""
    return [
        toolcall("ls -a && cat buggy.py && cat test_buggy.py", "Read the buggy file and its test."),
        toolcall(
            "sed -i.bak 's/items\\[len(items)\\]/items[len(items) - 1]/' buggy.py && rm -f buggy.py.bak && cat buggy.py",
            "Fix the off-by-one and show the result.",
        ),
        toolcall("harness_submit_patch", "Test evidence is green; submit via the harness command."),
    ]


def verbose_fixture_scenario() -> list[dict]:
    """A realistically messy run: duplicates, stale big outputs, then the fix.

    Used by the shadow-mode compaction study (Phase 2.6): the conversation grows far
    past the compaction target so that verdicts and pruning can be observed.
    """
    steps = [
        toolcall("ls -la && find . -type f", "Survey the repository."),
        toolcall("cat buggy.py && cat buggy.py", "Read the buggy file (twice, as models do)."),
        toolcall("yes 'debug log line' | head -400", "Produce a large, stale debugging output."),
        toolcall("cat README.md && cat test_buggy.py", "Read the README and the test."),
        toolcall("python -m pytest -q", "Run the test suite to see the failure."),
        toolcall("cat buggy.py", "Re-read the file to be sure."),
        toolcall("yes 'more debug noise' | head -400", "Another large stale output."),
        toolcall("nl -ba buggy.py | sed -n '1,20p'", "Look at line numbers before editing."),
        toolcall("python -c \"import buggy; print(buggy.last_item([1,2,3]))\"", "Reproduce the bug."),
        toolcall("sed -i.bak 's/items\\[len(items)\\]/items[len(items) - 1]/' buggy.py && rm -f buggy.py.bak", "Apply the fix."),
        toolcall("python -m pytest -q", "Verify the fix."),
        toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "Submit."),
    ]
    return steps


def build_dry_run_model(scenario: list[dict] | None = None, observation_template: str = "", telemetry=None):
    outputs = list(scenario) if scenario else default_scenario()
    # A deterministic model must never run out of script: pad with a submit.
    outputs.append(toolcall("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "Fallback submit.", call_id="call_fallback"))
    inner = DeterministicToolcallModel(
        outputs=outputs,
        model_name="dry-run",
        cost_per_call=0.0,
        observation_template=observation_template or DEFAULT_OBSERVATION_TEMPLATE,
    )
    return RelayedModel(inner, telemetry)


class RelayedModel:
    """Wrap any model to emit the same telemetry events a live model would."""

    def __init__(self, inner, telemetry=None):
        self.inner = inner
        self.telemetry = telemetry
        self.config = inner.config  # AgentConfig/serialize compatibility

    def query(self, messages: list[dict], **kwargs) -> dict:
        started = time.time()
        message = self.inner.query(messages, **kwargs)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}
        message.setdefault("extra", {})["harness_usage"] = usage
        if self.telemetry is not None:
            self.telemetry.emit(
                "model_call",
                latency_s=round(time.time() - started, 3),
                prompt_messages=len(messages),
                prompt_chars=sum(len(str(m.get("content") or "")) for m in messages),
                cost=0.0,
                usage=usage,
                dry_run=True,
            )
        return message

    def format_message(self, **kwargs) -> dict:
        return self.inner.format_message(**kwargs)

    def format_observation_messages(self, message, outputs, template_vars=None) -> list[dict]:
        return self.inner.format_observation_messages(message, outputs, template_vars)

    def get_template_vars(self, **kwargs) -> dict:
        return self.inner.get_template_vars(**kwargs)

    def serialize(self) -> dict:
        data = self.inner.serialize()
        data.setdefault("info", {}).setdefault("config", {})["relayed"] = True
        return data


def scenario_header() -> dict:
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "dry-run"}
