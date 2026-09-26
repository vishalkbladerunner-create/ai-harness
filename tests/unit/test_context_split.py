"""Context split: token counting, section attribution, telemetry wiring."""

from __future__ import annotations

import json

from harness.context.tokens import (
    TOOLS_HEADING,
    bash_schema_tokens,
    count_tokens,
    split_context,
)
from harness.dryrun import build_dry_run_model, toolcall
from harness.telemetry import Telemetry


def test_count_tokens_is_positive_and_monotonic():
    assert count_tokens("") == 0
    small = count_tokens("hello world")
    assert 0 < small < count_tokens("hello world " * 50)


def test_split_sections_sum_to_total_and_free_is_derived():
    messages = [
        {"role": "system", "content": "You are a guarded agent. " * 20},
        {"role": "user", "content": "Please fix the failing test. " * 10},
        {"role": "assistant", "content": "I will inspect the repository. " * 5},
    ]
    split = split_context(messages, limit=10_000)
    assert split["system"] > 0 and split["messages"] > 0
    assert split["system"] + split["tools"] + split["messages"] == split["total"]
    assert split["total"] < 10_000
    assert split["utilization"] == round(split["total"] / 10_000, 4)
    assert split["method"]


def test_tools_section_is_extracted_from_the_message_it_lives_in():
    prompt = (
        "Intro text about the task.\n\n"
        f"{TOOLS_HEADING}\n\n"
        "* `harness_submit_patch` — finish the run.\n\n"
        "## Next section\n\nmore text\n"
    )
    from harness.context.tokens import heading_section

    expected_tools = count_tokens(heading_section(prompt))
    with_tools = split_context([{"role": "user", "content": prompt}], limit=1000)
    assert expected_tools > 0
    assert with_tools["tools"] == expected_tools
    # the section is attributed to tools, not double-counted in messages
    assert with_tools["messages"] == count_tokens(prompt) - expected_tools
    assert with_tools["total"] == with_tools["system"] + with_tools["tools"] + with_tools["messages"]


def test_bash_schema_tokens_counts_upstreams_tool_schema():
    assert bash_schema_tokens() > 0


def test_dry_run_model_emits_context_split_with_configured_limit(tmp_path):
    telemetry = Telemetry(tmp_path / "telemetry.jsonl", "unit-context")
    model = build_dry_run_model([toolcall("echo hi")], telemetry=telemetry, context_limit=4096)
    model.query(
        [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user prompt"},
        ]
    )
    telemetry.close()
    events = [json.loads(line) for line in (tmp_path / "telemetry.jsonl").read_text().splitlines()]
    split = events[0]["context_split"]
    assert events[0]["kind"] == "model_call"
    assert split["limit"] == 4096
    assert split["system"] == count_tokens("system prompt")
    assert split["messages"] == count_tokens("user prompt")
    assert split["tools"] >= bash_schema_tokens()
    assert 0 < split["utilization"] < 1
