"""Guarded environment: secret stripping, masking, sentinels, refusals, elision."""

from __future__ import annotations

from pathlib import Path

from harness.environment import GuardedEnvironment


class RefuseAll:
    def check(self, command: str) -> dict:
        return {"outcome": "refuse", "reason": "unit-test policy", "suggestion": "try something safer", "probability": 0.99, "source": "test"}


class AllowAll:
    def check(self, command: str) -> dict:
        return {"outcome": "allow", "reason": "unit-test policy", "source": "test"}


def make_env(tmp_path: Path, **kwargs) -> GuardedEnvironment:
    return GuardedEnvironment(workspace=tmp_path, **kwargs)


def test_executes_and_returns_output(tmp_path):
    output = make_env(tmp_path).execute({"command": "echo hello"})
    assert output["returncode"] == 0
    assert "hello" in output["output"]


def test_credential_env_values_are_stripped_from_child(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SECRET_TOKEN", "supersecretvalue123")
    monkeypatch.setenv("TEST_PUBLIC_VAR", "visible-value")
    output = make_env(tmp_path).execute(
        {"command": "echo \"secret=[$(printenv TEST_SECRET_TOKEN)] public=[$(printenv TEST_PUBLIC_VAR)]\""}
    )
    assert "supersecretvalue123" not in output["output"]
    assert "secret=[]" in output["output"]  # blanked, not merely masked
    assert "public=[visible-value]" in output["output"]  # non-credentials pass through


def test_output_is_masked_against_credential_shapes(tmp_path):
    output = make_env(tmp_path).execute({"command": "echo sk-abcdefghijklmnopqrstuvwxyz123456"})
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in output["output"]
    assert "[REDACTED]" in output["output"]


def test_refusal_is_graceful_observation(tmp_path):
    output = make_env(tmp_path, policy=RefuseAll()).execute({"command": "rm -rf /"})
    assert output["returncode"] == 126
    assert "POLICY REFUSAL" in output["output"]
    assert "try something safer" in output["output"]


def test_allow_policy_executes(tmp_path):
    output = make_env(tmp_path, policy=AllowAll()).execute({"command": "echo allowed"})
    assert output["returncode"] == 0
    assert "allowed" in output["output"]


def test_unknown_sentinel_is_explained(tmp_path):
    output = make_env(tmp_path, sentinels={"harness_load_issue": lambda arg: {"output": "ISSUE"}}).execute(
        {"command": "harness_submit_patch"}
    )
    assert output["returncode"] == 1
    assert "Unknown harness command" in output["output"]
    assert "harness_load_issue" in output["output"]


def test_known_sentinel_dispatches(tmp_path):
    output = make_env(tmp_path, sentinels={"harness_load_issue": lambda arg: {"output": "ISSUE TEXT"}}).execute(
        {"command": "harness_load_issue"}
    )
    assert output["returncode"] == 0
    assert "ISSUE TEXT" in output["output"]


def test_long_output_is_elided(tmp_path):
    env = make_env(tmp_path, max_output_chars=200)
    output = env.execute({"command": "python3 -c \"print('x' * 5000)\""})
    assert "<elided" in output["output"]
    assert len(output["output"]) < 5000


def test_telemetry_records_commands(tmp_path):
    from harness.telemetry import Telemetry

    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    env = make_env(tmp_path, telemetry=telemetry)
    env.execute({"command": "echo one"})
    events = telemetry.events_of("command")
    assert events and events[0]["command"] == "echo one"
    telemetry.close()
