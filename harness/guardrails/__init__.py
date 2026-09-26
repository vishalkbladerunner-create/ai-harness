"""
Guardrail layer (Phase 1).

Modules:
  * ``policy`` — deterministic blocklist/allowlist + laya second opinion, the
    act/ask/refuse decision with calibrated probability.
  * ``scope`` — in-scope file policy, end-of-run diff check and rollback.
  * ``injection`` — untrusted-content scanning and quarantine.
  * ``secrets`` re-exported from ``harness.secrets`` (single home).
"""

from harness.secrets import contains_credential_like, mask_text  # noqa: F401
