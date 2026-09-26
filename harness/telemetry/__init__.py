"""Telemetry package: append-only JSONL logging + end-of-run Markdown reporting."""

from harness.telemetry.log import Telemetry, read_events

__all__ = ["Telemetry", "read_events"]
