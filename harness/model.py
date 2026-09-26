"""
Model wrapper — one thin layer over upstream's litellm model.

Why it exists:
  * Reproducibility: pinned temperature/seed live in checked-in config; if an
    endpoint rejects a parameter (many OpenAI-compatible servers reject `seed`),
    we degrade once, record it in telemetry, and continue — never crash the run.
  * Accounting: normalise token usage onto the message so the agent's budget
    tracker and the report do not have to dig through raw provider payloads.
  * Attribution: the wrapper is additive; upstream behaviour is untouched.

Hackathon criteria served: rule 9 (pinned temperature/seed, documented),
Phase 1.4 (hard token budget needs trustworthy usage), Phase 4.1 (latency/cost
per call).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import litellm

from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel

from harness.secrets import redact_sensitive_fields

logger = logging.getLogger("harness.model")

#: Substrings that indicate a server refused one of the pinned parameters.
_UNSUPPORTED_HINTS = (
    "unsupported",
    "not supported",
    "unrecognized",
    "unknown",
    "unexpected keyword",
    "extra fields not permitted",
    "invalid parameter",
)
_FALLBACK_PARAMS = ("seed", "temperature", "max_tokens")


def _num(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_usage(message: dict) -> dict:
    """Normalise provider usage into {prompt_tokens, completion_tokens, total_tokens}.

    Falls back to a character-based estimate when the endpoint omits usage; the
    report marks estimated numbers so nobody mistakes them for billed tokens.
    """
    usage = {}
    response = message.get("extra", {}).get("response")
    raw = None
    if isinstance(response, dict):
        raw = response.get("usage")
    elif response is not None:
        raw = getattr(response, "usage", None)

    def pick(source, *names):
        for name in names:
            if isinstance(source, dict) and source.get(name) is not None:
                return source[name]
            if source is not None and getattr(source, name, None) is not None:
                return getattr(source, name)
        return None

    prompt = _num(pick(raw, "prompt_tokens", "input_tokens"))
    completion = _num(pick(raw, "completion_tokens", "output_tokens"))
    total = _num(pick(raw, "total_tokens"))
    if prompt is None and completion is None:
        text = "".join(str(m.get("content") or "") for m in message.get("extra", {}).get("prompt_messages", []))
        approx = max(1, len(text) // 4)
        return {
            "prompt_tokens": approx,
            "completion_tokens": max(1, len(str(message.get("content") or "")) // 4),
            "total_tokens": approx + max(1, len(str(message.get("content") or "")) // 4),
            "estimated": True,
        }
    prompt = prompt or 0
    completion = completion or 0
    total = total or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated": False,
    }


class HarnessModel(LitellmModel):
    """Additive wrapper: telemetry, usage normalisation, one-shot param fallback."""

    def __init__(self, *, telemetry=None, provider_chain=None, **kwargs):
        self._telemetry = telemetry
        self._degradations: list[str] = []
        self._provider_chain = [dict(entry) for entry in (provider_chain or [])]
        super().__init__(**kwargs)
        self.active_model_name = self.config.model_name
        self.active_base_url = str((self.config.model_kwargs or {}).get("api_base", ""))

    # -- provider discovery: the evaluator's key may belong to DeepSeek or Qwen --
    def _try_next_provider(self, reason: str) -> bool:
        """Switch to the next checked-in provider. Returns False when exhausted."""
        if not self._provider_chain:
            return False
        entry = self._provider_chain.pop(0)
        previous = self.active_model_name
        self.config.model_name = entry["model_name"]
        self.config.model_kwargs["api_base"] = entry["base_url"]
        self.active_model_name = entry["model_name"]
        self.active_base_url = entry["base_url"]
        if self._telemetry is not None:
            self._telemetry.emit(
                "provider_fallback",
                reason=reason,
                previous_model=previous,
                model=self.active_model_name,
                base_url=self.active_base_url,
            )
            self._telemetry.degrade(
                "model", f"{reason} on {previous}; switched to {self.active_model_name} (provider discovery)"
            )
        return True

    # -- fallback: server rejects pinned sampling params --------------------
    def _query(self, messages: list[dict], **kwargs):
        try:
            return super()._query(messages, **kwargs)
        except litellm.exceptions.BadRequestError as exc:
            text = str(exc).lower()
            if not any(hint in text for hint in _UNSUPPORTED_HINTS):
                raise
            rejected = [p for p in _FALLBACK_PARAMS if p in text and p in self.config.model_kwargs]
            if not rejected:
                raise
            self._note_degradation(
                f"endpoint rejected pinned parameter(s) {rejected}; retried once without them (reproducibility degraded)"
            )
            for param in rejected:
                self.config.model_kwargs.pop(param, None)
            return super()._query(messages, **kwargs)

    def _note_degradation(self, reason: str) -> None:
        self._degradations.append(reason)
        if self._telemetry is not None:
            self._telemetry.degrade("model", reason)

    # -- accounting + telemetry --------------------------------------------
    def query(self, messages: list[dict], **kwargs) -> dict:
        started = time.time()
        n_messages = len(messages)
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        try:
            message = super().query(messages, **kwargs)
        except FormatError as exc:
            if self._telemetry is not None:
                self._telemetry.emit(
                    "model_call",
                    call=exc.messages[0].get("extra", {}).get("api_calls") if exc.messages else None,
                    latency_s=round(time.time() - started, 3),
                    prompt_messages=n_messages,
                    prompt_chars=prompt_chars,
                    format_error=True,
                )
            raise
        except (litellm.exceptions.AuthenticationError, litellm.exceptions.NotFoundError) as exc:
            # The evaluator's key may belong to the other provider; try the next
            # checked-in default (empty when MODEL_BASE_URL/MODEL_NAME were explicit).
            if self._try_next_provider(type(exc).__name__):
                return self.query(messages, **kwargs)
            if isinstance(exc, litellm.exceptions.AuthenticationError):
                # Never echo key material in an error path.
                raise RuntimeError(
                    "model endpoint rejected the credential (AuthenticationError). "
                    "Check that AI_API_KEY matches MODEL_BASE_URL; the value is never logged."
                ) from None
            raise
        except litellm.exceptions.BadRequestError as exc:
            # Some providers answer an unavailable model name with 400 rather than 404.
            text = str(exc).lower()
            if "model" in text and any(hint in text for hint in ("not found", "not exist", "no such", "unknown model")):
                if self._try_next_provider("BadRequestError(model unavailable)"):
                    return self.query(messages, **kwargs)
            raise
        usage = extract_usage(message)
        message.setdefault("extra", {})["harness_usage"] = usage
        if self._telemetry is not None:
            self._telemetry.emit(
                "model_call",
                latency_s=round(time.time() - started, 3),
                prompt_messages=n_messages,
                prompt_chars=prompt_chars,
                cost=message.get("extra", {}).get("cost", 0.0),
                usage=usage,
            )
        return message

    def get_template_vars(self, **kwargs) -> dict:
        # Credential-safe: the raw api_key must never reach a prompt or a log.
        return redact_sensitive_fields(self.config.model_dump())

    def serialize(self) -> dict:
        """Serialization used by the agent's trajectory (never contains the key).

        Upstream dumps ``self.config`` into ``trajectory.json`` on every step;
        the api_key lives in ``model_kwargs``, so it would otherwise be persisted
        to disk. Redact by field name at the source.
        """
        return redact_sensitive_fields(super().serialize())
