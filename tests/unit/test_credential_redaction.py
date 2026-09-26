"""Credential hygiene: no runtime credential may reach a persisted artefact.

Regression tests for the trajectory leak: upstream serializes the model config
(which contains ``model_kwargs.api_key``) into ``trajectory.json`` on every step.
"""

from __future__ import annotations

import json

from minisweagent.agents.default import AgentConfig

from harness.agent import HarnessAgent
from harness.environment import GuardedEnvironment
from harness.model import HarnessModel
from harness.secrets import mask_obj, redact_sensitive_fields

FAKE_KEY = "sk-review-secret-abcdef1234567890"


def make_model() -> HarnessModel:
    return HarnessModel(
        telemetry=None,
        observation_template="{{ output.output }}",
        format_error_template="{{ error }}",
        model_name="openai/mock-model",
        model_kwargs={
            "api_base": "http://127.0.0.1:9/v1",
            "api_key": FAKE_KEY,
            "temperature": 0.0,
            "seed": 42,
            "max_tokens": 4096,
        },
        cost_tracking="ignore_errors",
    )


def test_model_serialize_redacts_api_key():
    data = json.dumps(make_model().serialize())
    assert FAKE_KEY not in data
    assert "[REDACTED]" in data


def test_model_template_vars_redact_api_key():
    assert FAKE_KEY not in json.dumps(make_model().get_template_vars())


def test_redact_sensitive_fields_keeps_non_secrets():
    data = redact_sensitive_fields(
        {"model_kwargs": {"api_key": "opaque-token-value", "temperature": 0.0, "max_tokens": 4096}}
    )
    assert data["model_kwargs"]["api_key"] == "[REDACTED]"
    assert data["model_kwargs"]["temperature"] == 0.0
    assert data["model_kwargs"]["max_tokens"] == 4096


def test_mask_obj_masks_env_values(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "super-secret-value-12345")
    assert "super-secret-value-12345" not in json.dumps(mask_obj({"output": "super-secret-value-12345"}))
    assert "[REDACTED]" in json.dumps(mask_obj({"output": "super-secret-value-12345"}))


def test_agent_serialize_never_carries_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "super-secret-value-12345")
    env = GuardedEnvironment(workspace=tmp_path, telemetry=None, policy=None, sentinels={}, postprocessors=[])
    agent = HarnessAgent(
        make_model(), env, config_class=AgentConfig, system_template="system", instance_template="{{task}}"
    )
    data = json.dumps(agent.serialize())
    assert FAKE_KEY not in data
    assert "super-secret-value-12345" not in data
