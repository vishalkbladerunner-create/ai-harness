"""Provider discovery: the evaluator's key may belong to DeepSeek or Qwen.

Explicit MODEL_BASE_URL/MODEL_NAME disable the fallback (no substitution);
with only AI_API_KEY the harness switches provider once on a 401/404.
"""

from __future__ import annotations

import litellm
import pytest

from harness.model import HarnessModel
from harness.telemetry import Telemetry

QWEN = {"model_name": "openai/qwen3.7-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}


def make_model(telemetry=None, chain=None) -> HarnessModel:
    return HarnessModel(
        telemetry=telemetry,
        observation_template="{{ output.output }}",
        format_error_template="{{ error }}",
        model_name="openai/deepseek-flash",
        model_kwargs={"api_base": "https://api.deepseek.com/v1", "api_key": "sk-test", "temperature": 0.0},
        cost_tracking="ignore_errors",
        provider_chain=chain or [],
    )


def test_try_next_provider_switches_endpoint(tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])
    assert model.active_model_name == "openai/deepseek-flash"
    assert model._try_next_provider("AuthenticationError") is True
    assert model.active_model_name == "openai/qwen3.7-plus"
    assert model.config.model_kwargs["api_base"].startswith("https://dashscope")
    assert model._try_next_provider("AuthenticationError") is False
    events = telemetry.events_of("provider_fallback")
    assert events and events[0]["model"] == "openai/qwen3.7-plus"
    telemetry.close()


def test_query_retries_with_next_provider_on_auth_error(monkeypatch, tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])
    calls: list[str] = []

    def fake_query(self, messages, **kwargs):
        calls.append(self.config.model_name)
        if len(calls) == 1:
            raise litellm.exceptions.AuthenticationError(message="bad key", llm_provider="openai", model="deepseek-flash")
        return {"role": "assistant", "content": "ok", "extra": {"actions": []}}

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    message = model.query([{"role": "user", "content": "hi"}])
    assert message["content"] == "ok"
    assert calls == ["openai/deepseek-flash", "openai/qwen3.7-plus"]
    assert model.active_model_name == "openai/qwen3.7-plus"
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
                message="model qwen3.7-plus not found", llm_provider="openai", model="qwen3.7-plus"
            )
        return {"role": "assistant", "content": "ok", "extra": {"actions": []}}

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    message = model.query([{"role": "user", "content": "hi"}])
    assert message["content"] == "ok"
    assert model.active_model_name == "openai/qwen3.7-plus"
    telemetry.close()


def test_auth_failure_skips_other_models_on_the_same_provider(tmp_path):
    """A Qwen key must not walk every DeepSeek model before reaching DashScope."""
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    same_provider = {"model_name": "openai/deepseek-flash", "base_url": "https://api.deepseek.com/v1"}
    model = make_model(telemetry, [same_provider, dict(QWEN)])
    assert model._try_next_provider("AuthenticationError", skip_same_provider=True) is True
    assert model.active_model_name == "openai/qwen3.7-plus"
    assert [entry["model_name"] for entry in model._provider_chain] == []
    telemetry.close()


def test_insufficient_balance_is_a_clear_error_and_does_not_switch(monkeypatch, tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])

    def fake_query(self, messages, **kwargs):
        raise litellm.exceptions.BadRequestError(
            message="Insufficient Balance (request_id: abc)", llm_provider="openai", model="deepseek-flash"
        )

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    with pytest.raises(RuntimeError) as excinfo:
        model.query([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "insufficient balance" in message.lower()
    assert "sk-test" not in message
    # the credential must not be sent to the other provider on a balance error
    assert model.active_model_name == "openai/deepseek-flash"
    assert [entry["model_name"] for entry in model._provider_chain] == ["openai/qwen3.7-plus"]
    telemetry.close()


def test_4xx_errors_are_not_retried():
    assert litellm.exceptions.BadRequestError in HarnessModel.abort_exceptions


def test_discovery_mode_exhaustion_names_the_supported_providers(monkeypatch, tmp_path):
    """Both official providers rejected the key: the message must say so plainly."""
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])

    def fake_query(self, messages, **kwargs):
        raise litellm.exceptions.AuthenticationError(message="bad key", llm_provider="openai", model="x")

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    with pytest.raises(RuntimeError) as excinfo:
        model.query([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "DeepSeek" in message and "Qwen" in message
    assert "official" in message
    assert "sk-test" not in message
    telemetry.close()


def test_discovery_mode_404_exhaustion_also_names_the_providers(monkeypatch, tmp_path):
    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    model = make_model(telemetry, [dict(QWEN)])

    def fake_query(self, messages, **kwargs):
        raise litellm.exceptions.NotFoundError(message="no such model", llm_provider="openai", model="x")

    monkeypatch.setattr("minisweagent.models.litellm_model.LitellmModel.query", fake_query)
    with pytest.raises(RuntimeError) as excinfo:
        model.query([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "DeepSeek" in message and "Qwen" in message
    assert "sk-test" not in message
    telemetry.close()


def test_extract_usage_captures_deepseek_cache_fields():
    from harness.model import extract_usage

    message = {
        "role": "assistant",
        "content": "ok",
        "extra": {
            "response": {
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "total_tokens": 1100,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 200,
                }
            }
        },
    }
    usage = extract_usage(message)
    assert usage["prompt_tokens"] == 1000
    assert usage["cache_hit_tokens"] == 800
    assert usage["cache_miss_tokens"] == 200
    assert usage["estimated"] is False


def test_extract_usage_supports_the_openai_nested_cache_shape():
    from harness.model import extract_usage

    message = {
        "role": "assistant",
        "content": "ok",
        "extra": {
            "response": {
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 50,
                    "total_tokens": 550,
                    "prompt_tokens_details": {"cached_tokens": 300},
                }
            }
        },
    }
    assert extract_usage(message)["cache_hit_tokens"] == 300


def test_extract_usage_estimated_path_has_zero_cache():
    from harness.model import extract_usage

    usage = extract_usage({"role": "assistant", "content": "hello world", "extra": {}})
    assert usage["estimated"] is True
    assert usage["cache_hit_tokens"] == 0
