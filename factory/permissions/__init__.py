"""Permission system.

The "same access as me" principle: the factory may do exactly what you could do
by hand - no more, and never without asking when it matters.

    gate.py       risk classification + allowlists + rate limits + decisions
    approval.py   human-in-the-loop callbacks (CLI prompt, Windows popup)
    allowlist.json  the editable rules: recipients, domains, repos, limits

Design rules baked in here:
  1. Fail-safe DENY - unknown action, unlisted recipient, missing config => no.
  2. Critical actions are always blocked, never merely "ask".
  3. Every decision (allow, deny, timeout, block) is written to the
     SHA-256 hash-chained audit log, so the trail cannot be edited quietly.
  4. The global kill switch (Ctrl+Shift+Q) overrides everything, instantly.
"""

from .gate import (  # noqa: F401
    ACTION_RISK,
    Decision,
    Denied,
    Gate,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    default_gate,
    risk_of,
)
from .approval import ApprovalResult, Approver  # noqa: F401

__all__ = [
    "Gate", "Denied", "Decision", "ACTION_RISK", "risk_of", "default_gate",
    "Approver", "ApprovalResult",
    "RISK_LOW", "RISK_MEDIUM", "RISK_HIGH", "RISK_CRITICAL",
]
