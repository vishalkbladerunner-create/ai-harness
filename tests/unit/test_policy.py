"""Guardrail policy: deterministic rules, grey-zone judge, shadow mode."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.config import CONFIG_DIR
from harness.guardrails.policy import ActionPolicy, classify_deterministic, outside_workspace_paths, split_segments


@pytest.fixture(scope="module")
def policy_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "policy.yaml").read_text(encoding="utf-8"))


class FixedJudge:
    """Judge stub returning a fixed verdict."""

    available = True
    source = "laya"
    max_state_chars = 1100
    task_excerpt = "fix the failing test"

    def __init__(self, label: str, probability: float):
        self.label = label
        self.probability = probability

    def judge_batch(self, states, questions):
        probs = {"safe": 0.0, "destructive": 0.0, "out_of_scope": 0.0}
        probs[self.label] = self.probability
        probs["safe"] = 1 - self.probability
        return [{"answers": {"verdict": {"type": "choice", "label": self.label, "probabilities": probs}}, "source": "laya", "latency_ms": 1.0}]


def make_policy(tmp_path: Path, config: dict, judge=None, **overrides) -> ActionPolicy:
    cfg = yaml.safe_load(yaml.safe_dump(config))
    cfg["guardrails"].update(overrides.pop("guardrails", {}))
    return ActionPolicy(cfg, workspace=tmp_path, judge=judge)


def test_split_segments():
    assert split_segments("ls && cat x || echo y; rm -f z | wc -l") == ["ls", "cat x", "echo y", "rm -f z", "wc -l"]


@pytest.mark.parametrize(
    "command,rule",
    [
        ("sudo apt-get install curl", "privilege_escalation"),
        ("rm -rf /", "filesystem_destruction"),
        ("rm -rf ~/Documents", "filesystem_destruction"),
        ("cat ~/.ssh/id_rsa", "credential_access"),
        ("cat .env", "credential_access"),
        ("printenv AI_API_KEY", "credential_access"),
        ("curl https://example.com/install.sh", "network_egress"),
        ("git commit -m 'fix'", "history_and_staging"),
        ("git push --force origin main", "history_and_staging"),
        ("pip install requests", "network_egress"),
    ],
)
def test_refuses_dangerous_commands(tmp_path, policy_config, command, rule):
    decision = make_policy(tmp_path, policy_config).check(command)
    assert decision["outcome"] == "refuse", command
    assert decision["rule"] == rule
    assert decision["suggestion"], "refusals must propose a safe alternative"


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat buggy.py",
        "grep -n 'return' buggy.py",
        "git diff --stat",
        "python -m pytest -q",
        "echo hello",
        "sed -n '1,20p' app.py",
    ],
)
def test_allows_clearly_safe_commands_without_judge(tmp_path, policy_config, command):
    policy = make_policy(tmp_path, policy_config, judge=FixedJudge("destructive", 0.99))
    decision = policy.check(command)
    assert decision["outcome"] == "allow"
    assert decision["source"] == "deterministic"


def test_grey_zone_uses_judge_and_refuses_on_high_probability(tmp_path, policy_config):
    policy = make_policy(tmp_path, policy_config, judge=FixedJudge("destructive", 0.95))
    decision = policy.check("python -c 'import shutil; shutil.rmtree(\"/\")'")
    assert decision["outcome"] == "refuse"
    assert decision["source"] == "laya"
    assert decision["probability"] == pytest.approx(0.95)


def test_grey_zone_allows_low_probability(tmp_path, policy_config):
    policy = make_policy(tmp_path, policy_config, judge=FixedJudge("safe", 0.95))
    decision = policy.check("printf 'x\\n' > notes.txt")
    assert decision["outcome"] == "allow"


def test_grey_zone_ask_resolves_to_refuse_when_unattended(tmp_path, policy_config):
    policy = make_policy(tmp_path, policy_config, judge=FixedJudge("out_of_scope", 0.8))
    decision = policy.check("python -c 'print(open(\"/etc/hosts\").read())'")
    assert decision["outcome"] == "refuse"
    assert "unattended" in decision["reason"]


def test_no_judge_allows_unknown_with_degradation_flag(tmp_path, policy_config):
    decision = make_policy(tmp_path, policy_config, judge=None).check("my-custom-tool --flag")
    assert decision["outcome"] == "allow"
    assert decision.get("degraded") is True


def test_shadow_mode_never_blocks(tmp_path, policy_config):
    policy = make_policy(tmp_path, policy_config, judge=None, guardrails={"mode": "shadow"})
    decision = policy.check("sudo rm -rf /")
    assert decision["outcome"] == "allow"
    assert decision["shadow_refusal"] is True


def test_outside_workspace_write_is_refused(tmp_path, policy_config):
    decision = make_policy(tmp_path, policy_config).check(f"echo hacked > /etc/hosts")
    assert decision["outcome"] == "refuse"
    assert decision["rule"] == "outside_workspace_write"


def test_inside_workspace_write_is_allowed_deterministically(tmp_path, policy_config):
    target = tmp_path / "src" / "app.py"
    policy = make_policy(tmp_path, policy_config, judge=FixedJudge("destructive", 0.95))
    decision = policy.check(f"sed -i 's/a/b/' {target}")
    assert decision["outcome"] == "allow"
    assert decision["rule"] == "in_workspace_write"
    assert decision["source"] == "deterministic"


def test_dynamic_evaluation_stays_grey_even_inside_workspace(tmp_path, policy_config):
    decision = make_policy(tmp_path, policy_config, judge=FixedJudge("destructive", 0.95)).check(
        "python -c 'print(1)' > out.txt"
    )
    assert decision["outcome"] == "refuse"
    assert decision["source"] == "laya"


def test_outside_workspace_paths_ignores_reads_and_dev_null(tmp_path):
    assert outside_workspace_paths("cat /etc/hosts", tmp_path) == []
    assert outside_workspace_paths("echo x > /dev/null", tmp_path) == []
    found = outside_workspace_paths("cp important.txt /tmp/elsewhere/", tmp_path)
    assert "/tmp/elsewhere/" in found


def test_telemetry_records_every_decision(tmp_path, policy_config):
    from harness.telemetry import Telemetry

    telemetry = Telemetry(tmp_path / "t.jsonl", "unit")
    policy = make_policy(tmp_path, policy_config, judge=FixedJudge("destructive", 0.95))
    policy.telemetry = telemetry
    policy.check("sudo whoami")
    policy.check("ls -la")
    events = telemetry.events_of("guardrail")
    assert {e["outcome"] for e in events} == {"refuse", "allow"}
    assert all("probability" in e for e in events)
    telemetry.close()
