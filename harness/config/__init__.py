"""
Configuration layer — reads checked-in YAML, never secrets.

Why it exists: hackathon compliance requires the model configuration to be
clearly defined in checked-in files with zero secrets, while the actual
credential/endpoint/model name arrive only through the environment at runtime.
This module is the *only* place that reads those environment variables, so
"where do secrets live?" has a one-line answer: nowhere — they are read here and
passed as an in-memory parameter to the model wrapper.

It also composes the prompts: the upstream mini-swe-agent system/instance
templates are loaded from the vendored package and *extended* (never replaced)
with our overlay, per Phase 5.2 of the build plan.

Hackathon criteria served: compliance 4 and 9 (checked-in, secret-free model
config; pinned temperature/seed), Phase 0.3 (load checked-in model config).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from minisweagent.config import builtin_config_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = CONFIG_DIR / "prompts"

#: Upstream config whose templates we extend. Vendored, read-only.
UPSTREAM_CONFIG = Path(builtin_config_dir) / "mini.yaml"

#: The evaluator supplies exactly one credential at runtime. Endpoint and model
#: name are optional: checked-in `model.provider_defaults` (harness.yaml) are used
#: when they are absent, so `export AI_API_KEY=... && make run` works untouched.
REQUIRED_ENV = ("AI_API_KEY",)
OPTIONAL_ENV = ("MODEL_BASE_URL", "MODEL_NAME")


class ConfigError(RuntimeError):
    """Raised for missing/invalid configuration. Message must be safe to print."""


def deep_merge(*dicts: dict) -> dict:
    """Recursively merge dicts; later values win. Lists are replaced, not merged."""
    out: dict = {}
    for d in dicts:
        for key, value in (d or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = deep_merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
    return out


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return data


def load_harness_config() -> dict:
    """Load harness.yaml, then optional gitignored local override."""
    config = load_yaml(CONFIG_DIR / "harness.yaml")
    local = CONFIG_DIR / "harness.local.yaml"
    if local.exists():
        config = deep_merge(config, load_yaml(local))
    return config


def load_policy_config() -> dict:
    """Load policy.yaml (guardrail rules + judge thresholds), with local override."""
    config = load_yaml(CONFIG_DIR / "policy.yaml")
    local = CONFIG_DIR / "policy.local.yaml"
    if local.exists():
        config = deep_merge(config, load_yaml(local))
    return config


def load_compaction_config() -> dict:
    """Load compaction.yaml (Phase 2), with local override. Missing file -> disabled."""
    path = CONFIG_DIR / "compaction.yaml"
    if not path.exists():
        return {"compaction": {"mode": "off"}}
    config = load_yaml(path)
    local = CONFIG_DIR / "compaction.local.yaml"
    if local.exists():
        config = deep_merge(config, load_yaml(local))
    return config


def read_env() -> dict[str, str]:
    """Read the evaluation environment variables. Returned, never written to disk.

    Only ``AI_API_KEY`` is required (that is what the committee exports). The
    endpoint/model variables are optional and fall back to checked-in defaults.
    """
    if not os.environ.get("AI_API_KEY"):
        raise ConfigError(
            "missing required environment variable: AI_API_KEY. Export the credential provided by "
            "the evaluator before running (see README 'Quickstart'); its value is never written to disk."
        )
    return {name: os.environ.get(name, "") for name in REQUIRED_ENV + OPTIONAL_ENV}


def openai_compatible_model_name(model_name: str) -> str:
    """Force an OpenAI-compatible route unless the caller already chose a provider.

    litellm needs a provider hint for arbitrary endpoints; ``openai/`` routes to
    the OpenAI-compatible client, and the actual base URL is supplied separately.
    """
    name = model_name.strip()
    if "/" in name:
        return name
    return f"openai/{name}"


def _default_providers(config: dict) -> list[dict]:
    """Checked-in provider defaults from harness.yaml (never secrets)."""
    entries = ((config.get("model") or {}).get("provider_defaults")) or []
    providers = []
    for entry in entries:
        if entry.get("base_url") and entry.get("model_name"):
            providers.append(
                {
                    "base_url": str(entry.get("base_url", "")).strip(),
                    "model_name": str(entry.get("model_name", "")).strip(),
                    "context_limit": int(entry.get("context_limit") or 0),
                }
            )
    return providers


def _infer_provider(text: str, defaults: list[dict]) -> dict | None:
    """Pick a default provider from a model name or base URL (qwen vs deepseek)."""
    low = (text or "").lower()
    for needle in ("qwen", "dashscope", "aliyun"):
        if needle in low:
            return next((d for d in defaults if "qwen" in d["model_name"].lower()), None)
    if "deepseek" in low:
        return next((d for d in defaults if "deepseek" in d["model_name"].lower()), None)
    return None


def provider_chain(config: dict, env: dict[str, str]) -> list[dict]:
    """Resolve the provider chain: explicit env wins, defaults otherwise.

    * ``MODEL_BASE_URL`` + ``MODEL_NAME`` both set -> exactly that endpoint
      (no fallback: the prescribed model is respected verbatim).
    * only one of them set -> the other is inferred from the checked-in
      defaults (e.g. ``MODEL_NAME=qwen-max`` picks the DashScope URL).
    * neither set -> the checked-in chain (DeepSeek first, then Qwen). The key
      only authenticates against its own provider, so the harness discovers the
      right one instead of failing on a 401; this is provider discovery, not
      model substitution, and it is disabled whenever the evaluator is explicit.
    """
    defaults = _default_providers(config)
    base_url = (env.get("MODEL_BASE_URL") or "").strip()
    model_name = (env.get("MODEL_NAME") or "").strip()
    if base_url and model_name:
        return [{"base_url": base_url, "model_name": model_name}]
    if model_name:
        inferred = _infer_provider(model_name, defaults)
        if inferred is None and defaults:
            inferred = defaults[0]
        if inferred is None:
            raise ConfigError("MODEL_NAME is set but no endpoint is known; set MODEL_BASE_URL as well")
        return [{"base_url": inferred["base_url"], "model_name": model_name}]
    if base_url:
        inferred = _infer_provider(base_url, defaults)
        if inferred is None and defaults:
            inferred = defaults[0]
        if inferred is None:
            raise ConfigError("MODEL_BASE_URL is set but no model name is known; set MODEL_NAME as well")
        return [{"base_url": base_url, "model_name": inferred["model_name"]}]
    if not defaults:
        raise ConfigError(
            "no model endpoint configured: export MODEL_BASE_URL and MODEL_NAME, or add "
            "model.provider_defaults to harness/config/harness.yaml"
        )
    return defaults


def build_model_config(config: dict, env: dict[str, str]) -> dict:
    """Assemble the kwargs for our model wrapper. Secret values stay in memory."""
    model_cfg = copy.deepcopy(config.get("model") or {})
    model_kwargs = dict(model_cfg.pop("model_kwargs", {}) or {})
    model_kwargs.pop("provider_defaults", None)
    # Context window for the split panel/report: explicit provider entries win,
    # then the checked-in default (never a hard-coded constant in code).
    default_context_limit = int(model_cfg.pop("context_limit", 0) or 0)
    chain = provider_chain(config, env)
    primary, fallbacks = chain[0], chain[1:]
    model_kwargs.update(
        {
            "api_base": primary["base_url"],
            "api_key": env["AI_API_KEY"],
        }
    )
    request_timeout = model_cfg.pop("request_timeout", None)
    if request_timeout is not None:
        model_kwargs.setdefault("timeout", request_timeout)
    return {
        "model_name": openai_compatible_model_name(primary["model_name"]),
        "model_kwargs": model_kwargs,
        "cost_tracking": model_cfg.get("cost_tracking", "ignore_errors"),
        "context_limit": primary.get("context_limit") or default_context_limit,
        "provider_chain": [
            {
                "model_name": openai_compatible_model_name(entry["model_name"]),
                "base_url": entry["base_url"],
                "context_limit": entry.get("context_limit") or default_context_limit,
            }
            for entry in fallbacks
        ],
    }


def build_prompts() -> dict[str, str]:
    """Compose upstream templates + our checked-in overlay (extend, not replace)."""
    upstream = load_yaml(UPSTREAM_CONFIG)
    upstream_agent = upstream.get("agent", {})
    system = upstream_agent.get("system_template", "")
    instance = upstream_agent.get("instance_template", "")
    system_extension = (PROMPTS_DIR / "system_extension.md").read_text(encoding="utf-8")
    instance_extension = (PROMPTS_DIR / "instance_extension.md").read_text(encoding="utf-8")
    return {
        "system_template": system.rstrip() + "\n\n" + system_extension.strip() + "\n",
        "instance_template": instance.rstrip() + "\n\n" + instance_extension.strip() + "\n",
        "observation_template": upstream.get("model", {}).get("observation_template", "{{ output.output }}"),
        "format_error_template": upstream.get("model", {}).get("format_error_template", "{{ error }}"),
    }
