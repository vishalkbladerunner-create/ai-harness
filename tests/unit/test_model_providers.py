"""Provider discovery: the evaluator's key may belong to DeepSeek or Qwen.

Explicit MODEL_BASE_URL/MODEL_NAME disable the fallback (no substitution);
with only AI_API_KEY the harness switches provider once on a 401/404.
"""

from __future__ import annotations

import litellm
import pytest

from harness.model import HarnessModel
from harness.telemetry import Telemetry

QWEN = {"model_name": "openai/qwen-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}


def make_model(telemetry=None, chain=None) -> HarnessModel:
    return HarnessModel(
        telemetry=telemetry,
        observation_template="{{ output.output }}",
        format_error_template="{{ error }}",
        model_name="openai/deepseek-chat",
        model_kwargs={"api_base": "https://api.deepseek.com/v1", "api_key": "sk-test", "temperature": 0.0},
        cost_tracking="ignore_errors",
        provider_chain=chain or [],
    )


def test_try_next_provider_switches_endpoint(tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])
    assert model.active_model_name == "openai/deepseek-chat"
    assert model._try_next_provider("AuthenticationError") is True
    assert model.active_model_name == "openai/qwen-plus"
    assert model.config.model_kwargs["api_base"].startswith("https://dashscope")
    assert model._try_next_provider("AuthenticationError") is False
    events = telemetry.events_of("provider_fallback")
    assert events and events[0]["model"] == "openai/qwen-plus"
    telemetry.close()


def test_query_retries_with_next_provider_on_auth_error(monkeypatch, tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])
    calls: list[str] = []

    def fake_query(self, messages, **kwargs):
        calls.append(self.config.model_name)
        if len(calls) == 1:
            raise litellm.exceptions.AuthenticationError(message="bad key", llm_provider="openai", model="deepseek-chat")
        return {"role": "assistant", "content": "ok", "extra": {"actions": []}}

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    message = model.query([{"role": "user", "content": "hi"}])
    assert message["content"] == "ok"
    assert calls == ["openai/deepseek-chat", "openai/qwen-plus"]
    assert model.active_model_name == "openai/qwen-plus"
    telemetry.close()


def test_query_without_chain_raises_a_safe_error(monkeypatch):
    model = make_model()

    def fake_query(self, messages, **kwargs):
        raise litellm.exceptions.AuthenticationError(message="bad key", llm_provider="openai", model="x")

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    with pytest.raises(RuntimeError) as excinfo:
        model.query([{"role": "user", "content": "hi"}])
    assert "sk-test" not in str(excinfo.value)
    assert "AuthenticationError" in str(excinfo.value)


def test_query_switches_on_model_unavailable_bad_request(monkeypatch, tmp_path):
    """Some providers answer an unavailable model name with 400, not 404."""
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])
    calls: list[str] = []

    def fake_query(self, messages, **kwargs):
        calls.append(self.config.model_name)
        if len(calls) == 1:
            raise litellm.exceptions.BadRequestError(
                message="model qwen-plus not found", llm_provider="openai", model="qwen-plus"
            )
        return {"role": "assistant", "content": "ok", "extra": {"actions": []}}

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    message = model.query([{"role": "user", "content": "hi"}])
    assert message["content"] == "ok"
    assert model.active_model_name == "openai/qwen-plus"
    telemetry.close()


def test_auth_failure_skips_other_models_on_the_same_provider(tmp_path):
    """A Qwen key must not walk every DeepSeek model before reaching DashScope."""
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    same_provider = {"model_name": "openai/deepseek-flash", "base_url": "https://api.deepseek.com/v1"}
    model = make_model(telemetry, [same_provider, dict(QWEN)])
    assert model._try_next_provider("AuthenticationError", skip_same_provider=True) is True
    assert model.active_model_name == "openai/qwen-plus"
    assert [entry["model_name"] for entry in model._provider_chain] == []
    telemetry.close()


def test_insufficient_balance_is_a_clear_error_and_does_not_switch(monkeypatch, tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])

    def fake_query(self, messages, **kwargs):
        raise litellm.exceptions.BadRequestError(
            message="Insufficient Balance (request_id: abc)", llm_provider="openai", model="deepseek-chat"
        )

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    with pytest.raises(RuntimeError) as excinfo:
        model.query([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "insufficient balance" in message.lower()
    assert "sk-test" not in message
    # the credential must not be sent to the other provider on a balance error
    assert model.active_model_name == "openai/deepseek-chat"
    assert [entry["model_name"] for entry in model._provider_chain] == ["openai/qwen-plus"]
    telemetry.close()


def test_4xx_errors_are_not_retried():
    assert litellm.exceptions.BadRequestError in HarnessModel.abort_exceptions
