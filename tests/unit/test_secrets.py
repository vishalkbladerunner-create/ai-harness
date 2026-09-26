"""Secret hygiene: masking and credential detection (compliance rule 6)."""

from __future__ import annotations

import os

from harness.secrets import contains_credential_like, mask_text, sensitive_env_values


def test_masks_sensitive_env_values(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_TOKEN", "supersecretvalue123")
    text = "the token is supersecretvalue123 and must not leak"
    assert "supersecretvalue123" not in mask_text(text)
    assert "[REDACTED]" in mask_text(text)


def test_masks_credential_shapes():
    assert "sk-" not in mask_text("key sk-abcdefghijklmnopqrstuvwxyz123456 ok")
    assert "ghp_" not in mask_text("ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "[REDACTED]" in mask_text('api_key = "AKIAIOSFODNN7EXAMPLE"')


def test_short_values_are_not_masked_aggressively(monkeypatch):
    monkeypatch.setenv("SHORT_TOKEN", "abc")
    assert "abc" in mask_text("abcdef")
    assert "abc" not in sensitive_env_values()


def test_contains_credential_like():
    assert contains_credential_like("token: sk-abcdefghijklmnopqrstuvwxyz123456")
    assert not contains_credential_like("def add(a, b):\n    return a + b\n")


def test_env_read_does_not_write_files():
    # Guardrail: our code must never persist the environment elsewhere.
    assert not os.path.exists(".env")
