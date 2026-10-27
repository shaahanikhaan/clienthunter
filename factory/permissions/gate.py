"""Central permission gate.

Every side-effecting action in the factory funnels through :meth:`Gate.check`.
The gate answers four questions, in order:

    1. Is the whole factory stopped (kill switch)?            -> deny
    2. Is this action in the agent's allowed tool list?       -> deny if not
    3. How risky is it?  critical -> block, high -> ask, medium -> ask/timeout,
       low -> allow.
    4. Are we inside the allowlist + daily rate limits?       -> deny if not

Nothing in the factory is allowed to bypass this.  The gate is deliberately
dependency-free (stdlib only) so it can never fail open because an optional
library is missing.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .. import FACTORY_ROOT

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"

#: Action -> intrinsic risk.  Anything not in this table is treated as HIGH
#: (fail-safe: unknown actions are never quietly allowed).
ACTION_RISK: Dict[str, str] = {
    # ---- low: read-only or local-only, no external effect ----------------
    "read_file": RISK_LOW,
    "write_sandbox": RISK_LOW,
    "list_files": RISK_LOW,
    "read_shared_data": RISK_LOW,
    "http_get": RISK_LOW,
    "search": RISK_LOW,
    "db_read": RISK_LOW,
    "db_write_local": RISK_LOW,
    "nlp_predict": RISK_LOW,
    "train_model": RISK_LOW,
    "build_demo_site": RISK_LOW,
    "git_status": RISK_LOW,
    "git_diff": RISK_LOW,
    "screen_capture": RISK_LOW,
    "mouse_move": RISK_LOW,
    "mouse_scroll": RISK_LOW,
    "audit_site": RISK_LOW,
    "run_command_read": RISK_LOW,        # dir, ipconfig, git status, Get-Process ...
    "run_command_write": RISK_MEDIUM,    # mkdir, copy, git commit, pip install ...
    "manage_agents": RISK_LOW,           # start/stop/pause a worker
    "system_info": RISK_LOW,             # read CPU/RAM/disk
    # ---- terminal: admin / destructive -> asks you first --------------------
    "run_command_admin": RISK_HIGH,      # del, Stop-Process, schtasks, reg ...
    "install_package": RISK_HIGH,
    # ---- terminal: never, not even with approval ----------------------------
    "run_command_critical": RISK_CRITICAL,   # format, diskpart, shutdown, rm -rf / ...
    # ---- medium: affects the user's own machine/accounts visibly ---------
    "browser_navigate": RISK_MEDIUM,
    "browser_click": RISK_MEDIUM,
    "browser_type": RISK_MEDIUM,
    "browser_fill_form": RISK_MEDIUM,
    "keyboard_type": RISK_MEDIUM,
    "write_file_outside_sandbox": RISK_MEDIUM,
    "create_branch": RISK_MEDIUM,
    "git_commit": RISK_MEDIUM,
    "scrape_maps": RISK_MEDIUM,
    "focus_window": RISK_MEDIUM,
    # ---- high: reaches another human, or changes shared state ------------
    "send_email": RISK_HIGH,
    "send_whatsapp": RISK_HIGH,
    "send_dm": RISK_HIGH,
    "post_public": RISK_HIGH,
    "comment_public": RISK_HIGH,
    "connect_request": RISK_HIGH,
    "git_push": RISK_HIGH,
    "file_delete": RISK_HIGH,
    "subprocess": RISK_HIGH,
    "credentials_access": RISK_HIGH,
    "publish_site": RISK_HIGH,
    # ---- critical: never, under any circumstances ------------------------
    "delete_recursive": RISK_CRITICAL,
    "format_disk": RISK_CRITICAL,
    "send_bulk_email": RISK_CRITICAL,
    "send_bulk_whatsapp": RISK_CRITICAL,
    "modify_permissions": RISK_CRITICAL,
    "disable_audit_log": RISK_CRITICAL,
    "exfiltrate_data": RISK_CRITICAL,
    "run_unsigned_binary": RISK_CRITICAL,
    "edit_system_files": RISK_CRITICAL,
}

#: Actions that consume a per-day quota, and the allowlist key holding the cap.
QUOTA_ACTIONS = {
    "send_email": ("email", "max_per_day"),
    "send_whatsapp": ("whatsapp", "max_per_day"),
    "send_dm": ("whatsapp", "max_per_day"),
    "connect_request": ("linkedin", "max_per_day"),
    "post_public": ("browser", "max_actions_per_minute"),
}


def risk_of(action: str) -> str:
    """Risk level of an action; unknown actions are HIGH (fail-safe)."""
    return ACTION_RISK.get(action, RISK_HIGH)


class Denied(PermissionError):
    """Raised by :meth:`Gate.enforce` when an action is not allowed."""


class Decision:
    """Outcome of a permission check."""

    __slots__ = ("allowed", "risk", "action", "agent", "reason", "needs_approval", "meta")

    def __init__(self, allowed: bool, risk: str, action: str, agent: str,
                 reason: str = "", needs_approval: bool = False, meta: Optional[Dict] = None):
        self.allowed = bool(allowed)
        self.risk = risk
        self.action = action
        self.agent = agent
        self.reason = reason
        self.needs_approval = needs_approval
        self.meta = meta or {}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed, "risk": self.risk, "action": self.action,
            "agent": self.agent, "reason": self.reason,
            "needs_approval": self.needs_approval, "meta": self.meta,
        }

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:  # pragma: no cover
        return "<Decision %s %s/%s allowed=%s>" % (self.agent, self.action, self.risk, self.allowed)


class Gate:
    """The single place where "may I do this?" is answered."""

    def __init__(self, allowlist: Optional[Dict[str, Any]] = None,
                 approver: Optional[Callable] = None,
                 audit: Optional[Any] = None,
                 path: Optional[Path] = None):
        self.path = Path(path) if path else FACTORY_ROOT / "permissions" / "allowlist.json"
        self.allowlist = allowlist if allowlist is not None else self._load()
        self.approver = approver
        self.audit = audit if audit is not None else _default_audit()
        self._counters: Dict[str, Dict[str, int]] = {}
        self._minute_counters: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._session_approvals: set = set()
        self.killed = False
        self.stats = {"allowed": 0, "denied": 0, "blocked": 0, "approved_by_human": 0,
                      "timed_out": 0}

    # ------------------------------------------------------------------ setup
    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"policy": {}, "agents": {}}

    def reload(self) -> None:
        self.allowlist = self._load()

    def policy(self) -> Dict[str, Any]:
        return self.allowlist.get("policy", {}) or {}

    # ------------------------------------------------------------ kill switch
    def kill(self, reason: str = "kill switch") -> None:
        self.killed = True
        if self.audit:
            self.audit.record("factory", "kill_switch", RISK_CRITICAL, "allow",
                              {"reason": reason})

    def revive(self) -> None:
        self.killed = False

    # ------------------------------------------------------------------- check
    def agent_tools(self, agent: str) -> Optional[list]:
        """Allowed tool list for an agent, or ``None`` when unrestricted."""
        cfg = (self.allowlist.get("agents", {}) or {}).get(agent, {})
        return cfg.get("allowed_tools")

    def check(self, agent: str, action: str, details: Optional[Dict[str, Any]] = None,
              interactive: Optional[bool] = None) -> Decision:
        """Decide whether ``agent`` may perform ``action``.  Never raises."""
        details = details or {}
        risk = risk_of(action)

        if self.killed:
            return self._log(agent, action, risk, False, "factory stopped (kill switch)")

        # 1) agent tool allowlist (fail-safe deny)
        tools = self.agent_tools(agent)
        if tools is not None and action not in tools:
            return self._log(agent, action, risk, False,
                             "action not in apt_%s's allowed_tools" % agent)

        # 2) critical => blocked, no human can override
        if risk == RISK_CRITICAL:
            self.stats["blocked"] += 1
            return self._log(agent, action, risk, False, "critical action blocked by policy")

        # 3) target allowlists (recipients / domains / repos)
        ok, why = self._check_allowlist(action, details)
        if not ok:
            return self._log(agent, action, risk, False, why)

        # 4) rate limits
        ok, why = self._check_quota(action, details)
        if not ok:
            return self._log(agent, action, risk, False, why)

        # 5) risk-based human approval
        if risk == RISK_LOW:
            return self._log(agent, action, risk, True, "low risk auto-approved")

        decision = self._ask_human(agent, action, risk, details, interactive)
        return decision

    def enforce(self, agent: str, action: str, details: Optional[Dict[str, Any]] = None,
                interactive: Optional[bool] = None) -> Decision:
        """Like :meth:`check` but raises :class:`Denied` when not allowed."""
        decision = self.check(agent, action, details, interactive)
        if not decision.allowed:
            raise Denied("[%s] %s denied: %s" % (agent, action, decision.reason))
        return decision

    # ------------------------------------------------------------ sub-checks
    def _check_allowlist(self, action: str, details: Dict[str, Any]):
        # email / whatsapp recipients
        if action in ("send_email",):
            return self._recipient_ok("email", details.get("to", ""))
        if action in ("send_whatsapp", "send_dm", "connect_request"):
            return self._recipient_ok("whatsapp", details.get("to", "") or details.get("phone", ""))
        # browser domains
        if action.startswith("browser_") or action in ("scrape_maps", "audit_site"):
            url = details.get("url", "") or details.get("domain", "")
            if url:
                return self._domain_ok(url)
        # github repos
        if action in ("git_push", "git_commit", "create_branch"):
            repo = details.get("repo", "")
            if repo:
                return self._repo_ok(repo)
        # filesystem writes outside the apartment sandbox
        if action in ("write_file_outside_sandbox", "file_delete", "delete_recursive"):
            path = str(details.get("path", ""))
            roots = (self.allowlist.get("filesystem", {}) or {}).get("writable_roots", [])
            if roots and not any(str(path).lower().startswith(str(r).lower()) for r in roots):
                return False, "path outside writable roots: %s" % path
        return True, ""

    def _recipient_ok(self, section: str, recipient: str):
        cfg = self.allowlist.get(section, {}) or {}
        if not cfg.get("enabled", True):
            return False, "%s transport disabled in allowlist.json" % section
        allowed = cfg.get("recipients", []) or []
        if "*" in allowed or cfg.get("allow_all", False):
            return True, ""
        if not recipient:
            return False, "no recipient supplied"
        if recipient in allowed:
            return True, ""
        # domain-level allowlisting
        if "@" in recipient:
            domain = recipient.split("@", 1)[1].lower()
            if domain in [d.lower() for d in cfg.get("domains", [])]:
                return True, ""
        return False, "recipient not in %s allowlist: %s" % (section, recipient)

    def _domain_ok(self, url: str):
        cfg = self.allowlist.get("browser", {}) or {}
        allowed = cfg.get("domains", []) or []
        if cfg.get("allow_all", False):
            return True, ""
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        host = host.split(":")[0]
        for entry in allowed:
            entry = entry.lower()
            if host == entry or host.endswith("." + entry):
                return True, ""
        # public search/scrape targets are always fine (read-only public data)
        if any(host.endswith(x) for x in ("google.com", "duckduckgo.com", "bing.com",
                                          "w3.org", "schema.org")):
            return True, ""
        return False, "domain not in browser allowlist: %s" % host

    def _repo_ok(self, repo: str):
        cfg = self.allowlist.get("github", {}) or {}
        allowed = cfg.get("repos", []) or []
        if "*" in allowed or cfg.get("allow_all", False):
            return True, ""
        return (True, "") if repo in allowed else (False, "repo not approved: %s" % repo)

    # ------------------------------------------------------------ rate limits
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _check_quota(self, action: str, details: Dict[str, Any]):
        if action not in QUOTA_ACTIONS:
            return True, ""
        section, key = QUOTA_ACTIONS[action]
        cfg = self.allowlist.get(section, {}) or {}
        limit = cfg.get(key)
        if not limit:
            return True, ""
        with self._lock:
            # per-minute throttle for browser-ish actions
            if key == "max_actions_per_minute":
                now = time.time()
                stamps = [t for t in self._minute_counters.get(action, []) if now - t < 60]
                if not isinstance(self._minute_counters.get(action), list):
                    self._minute_counters[action] = []
                self._minute_counters[action] = stamps
                if len(stamps) >= limit:
                    return False, "%s: %d actions/minute limit reached" % (action, limit)
                self._minute_counters[action].append(now)
                return True, ""
            day = self._today()
            used = self._counters.setdefault(day, {}).get(action, 0)
            if used >= int(limit):
                return False, "%s: daily limit of %d reached" % (action, limit)
            self._counters[day][action] = used + 1
        return True, ""

    def quota_usage(self) -> Dict[str, Any]:
        day = self._today()
        return {"date": day, "used": dict(self._counters.get(day, {}))}

    # ---------------------------------------------------------- human approval
    def _ask_human(self, agent: str, action: str, risk: str, details: Dict[str, Any],
                   interactive: Optional[bool]):
        policy = self.policy()
        session_key = "%s:%s" % (agent, action)
        if session_key in self._session_approvals:
            return self._log(agent, action, risk, True, "session auto-approval")

        auto = policy.get("auto_approve", {}) or {}
        if auto.get(risk):
            return self._log(agent, action, risk, True, "auto-approved by policy (%s)" % risk)

        interactive_default = bool(sys.stdin and sys.stdin.isatty())
        want_ask = interactive if interactive is not None else interactive_default
        if not self.approver or not want_ask:
            # Headless / CI: medium defaults to allow, high defaults to deny.
            fallback = policy.get("headless_default", {}) or {}
            allowed = bool(fallback.get(risk, risk == RISK_MEDIUM))
            self.stats["timed_out" if not allowed else "allowed"] += 0
            return self._log(agent, action, risk, allowed,
                             "headless policy: %s=%s" % (risk, "allow" if allowed else "deny"))

        timeout = int(policy.get("timeout_s", {}).get(
            risk, 10 if risk == RISK_MEDIUM else 60))
        on_timeout = policy.get("on_timeout", {}).get(risk,
                                                      "allow" if risk == RISK_MEDIUM else "deny")
        result = self.approver(agent=agent, action=action, risk=risk, details=details,
                               timeout=timeout)
        if result.approved:
            self.stats["approved_by_human"] += 1
            if result.remember:
                self._session_approvals.add(session_key)
            return self._log(agent, action, risk, True, "approved by human")
        if result.timed_out:
            self.stats["timed_out"] += 1
            allowed = on_timeout == "allow"
            return self._log(agent, action, risk, allowed,
                             "no answer in %ds -> %s" % (timeout, on_timeout))
        return self._log(agent, action, risk, False, "denied by human")

    # ------------------------------------------------------------------ audit
    def _log(self, agent: str, action: str, risk: str, allowed: bool, reason: str) -> Decision:
        if allowed:
            self.stats["allowed"] += 1
        else:
            self.stats["denied"] += 1
        if self.audit:
            try:
                self.audit.record(agent, action, risk, "allow" if allowed else "deny",
                                  {"reason": reason})
            except Exception:
                pass
        return Decision(allowed, risk, action, agent, reason)

    def report(self) -> Dict[str, Any]:
        return dict(self.stats, killed=self.killed, quota=self.quota_usage(),
                    session_approvals=sorted(self._session_approvals))


def _default_audit():
    """Lazy import to avoid a circular import at package load time."""
    try:
        from ..audit import AuditLog
        return AuditLog()
    except Exception:  # pragma: no cover - audit log unavailable
        return None


_GATE: Optional[Gate] = None
_GATE_LOCK = threading.Lock()


def default_gate(reload: bool = False) -> Gate:
    """Process-wide gate instance (so rate-limit counters are shared)."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None or reload:
            from .approval import Approver
            _GATE = Gate(approver=Approver().request)
        return _GATE
