"""The evaluator exports only AI_API_KEY; endpoint/model come from checked-in defaults."""

from __future__ import annotations

import pytest

from harness.config import ConfigError, build_model_config, load_harness_config, provider_chain, read_env


def _clear_model_env(monkeypatch) -> None:
    monkeypatch.setenv("AI_API_KEY", "sk-test-key-1234567890")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)


def test_read_env_requires_only_the_key(monkeypatch):
    _clear_model_env(monkeypatch)
    env = read_env()
    assert env["AI_API_KEY"] == "sk-test-key-1234567890"
    assert env["MODEL_BASE_URL"] == "" and env["MODEL_NAME"] == ""


def test_read_env_rejects_missing_key(monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        read_env()


def test_default_chain_is_deepseek_then_qwen(monkeypatch):
    _clear_model_env(monkeypatch)
    chain = provider_chain(load_harness_config(), read_env())
    assert [entry["model_name"] for entry in chain] == ["deepseek-chat", "qwen-plus"]
    assert chain[0]["base_url"].startswith("https://api.deepseek.com")
    assert "dashscope" in chain[1]["base_url"]


def test_explicit_env_wins_and_disables_fallback(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sk-x")
    monkeypatch.setenv("MODEL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MODEL_NAME", "mock-model")
    config = load_harness_config()
    assert provider_chain(config, read_env()) == [{"base_url": "http://127.0.0.1:9/v1", "model_name": "mock-model"}]
    assert build_model_config(config, read_env())["provider_chain"] == []


def test_model_name_infers_the_qwen_endpoint(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("MODEL_NAME", "qwen-max")
    chain = provider_chain(load_harness_config(), read_env())
    assert chain[0]["model_name"] == "qwen-max"
    assert "dashscope" in chain[0]["base_url"]


def test_base_url_infers_the_model_name(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("MODEL_BASE_URL", "https://api.deepseek.com/v1")
    chain = provider_chain(load_harness_config(), read_env())
    assert chain[0]["model_name"] == "deepseek-chat"


def test_build_model_config_prefixes_provider_and_keeps_chain(monkeypatch):
    _clear_model_env(monkeypatch)
    cfg = build_model_config(load_harness_config(), read_env())
    assert cfg["model_name"] == "openai/deepseek-chat"
    assert cfg["model_kwargs"]["api_key"] == "sk-test-key-1234567890"
    assert [entry["model_name"] for entry in cfg["provider_chain"]] == ["openai/qwen-plus"]
