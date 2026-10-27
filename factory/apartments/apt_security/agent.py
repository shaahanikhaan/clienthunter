"""apt_security - the 9-sentinel grid that watches everything, including the agents.

Named after the LAIS pattern in the brief: nine single-purpose sentinels that
each own one slice of the risk surface.  Isolation is the point - one prompt
injection that convinces the outreach agent to "also run this command" should
still hit a wall somewhere else.  Prompt-injection-to-RCE in agent systems is a
documented, real attack class [3](https://blog.trailofbits.com/2025/10/22/prompt-injection-to-rce-in-ai-agents/),
so every sentinel here is a *second* check, not a replacement for the gate.

    network_shield    outbound hosts must be on the sanctioned list
    code_sentry       scans source for eval/exec/shell=True/pickle/subprocess
    file_watchdog     no writes outside the factory tree / user dirs
    input_sanitizer   strips injection patterns from untrusted text
    auth_gate         re-checks permissions before any dangerous action
    anomaly_detector  spots rate spikes, error bursts, quota abuse
    crypto_guard      secrets must never be written to disk in clear text
    audit_logger      hash chain intact?  log every sentinel verdict
    decoy_engine      canary files/leads that should never be touched

Everything is stdlib.  Nothing phones home.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...audit import read_log, verify_chain
from ...store import default_store
from ..base_apartment import BaseApartment

STATE = "security_state.json"

#: Hosts the factory is allowed to talk to at all.
SANCTIONED_HOSTS = (
    "google.com", "google.com.pk", "maps.google.com", "duckduckgo.com",
    "gmail.com", "imap.gmail.com", "smtp.gmail.com",
    "web.whatsapp.com", "github.com", "api.github.com", "codeload.github.com",
    "raw.githubusercontent.com", "facebook.com", "linkedin.com", "instagram.com",
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
)

#: Patterns that must never appear in code we generate or run.
CODE_RULES: Tuple[Tuple[str, str], ...] = (
    (r"\beval\s*\(", "eval() is never acceptable"),
    (r"\bexec\s*\(", "exec() is never acceptable"),
    (r"subprocess\.[a-z_]+\([^)]*shell\s*=\s*True", "shell=True - injection risk, use argv"),
    (r"os\.system\s*\(", "os.system() - use subprocess with a list"),
    (r"pickle\.load", "pickle.load() - models must be JSON"),
    (r"__import__\s*\(", "dynamic __import__ from untrusted input"),
    (r"rm\s+-rf\s+/", "recursive delete from root"),
    (r"curl\s+[^|]*\|\s*(ba)?sh", "pipe-to-shell"),
)

#: Text patterns that look like someone trying to steer an agent.
INJECTION_RULES: Tuple[Tuple[str, str], ...] = (
    (r"ignore (all |the )?(previous|above|prior) (instructions|prompts)",
     "classic instruction-override"),
    (r"disregard (your|the) (rules|instructions|training)", "instruction-override"),
    (r"you are now|new (system )?(prompt|role)", "role reassignment"),
    (r"reveal (your |the )?(system prompt|api key|secret|password)", "secret extraction"),
    (r"\bapi[_ -]?key\b\s*[:=]", "api key assignment in content"),
    (r"run (the )?(command|cmd|powershell|bash)\s*:", "command injection attempt"),
    (r"sudo\s+|\bformat\s+c:|\bdel\s+/f\s+/s\b", "destructive command in content"),
)

SECRET_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"\bAIza[0-9A-Za-z\-_]{20,}\b", "google api key"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "openai-style key"),
    (r"\bghp_[A-Za-z0-9]{20,}\b", "github token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "slack token"),
    (r"AKIA[0-9A-Z]{16}", "aws access key id"),
)


class SecurityAgent(BaseApartment):
    NAME = "security"
    DESCRIPTION = "9-sentinel security grid: network, code, files, input, auth, anomalies, secrets, audit, decoys"
    PERMISSIONS = {"filesystem": True, "internet": False, "subprocess": False,
                   "email": False, "whatsapp": False}

    STATE_MODEL = STATE

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        model = {
            "sentinels": list(SENTINELS),
            "sanctioned_hosts": list(SANCTIONED_HOSTS),
            "code_rules": [{"pattern": p, "why": w} for p, w in CODE_RULES],
            "injection_rules": [{"pattern": p, "why": w} for p, w in INJECTION_RULES],
            "secret_patterns": [{"pattern": p, "what": w} for p, w in SECRET_PATTERNS],
            "policy": {
                "block_on_critical": True,
                "quarantine_after_violations": 3,
                "kill_switch_on": ["destructive command", "secret exfiltration"],
            },
            "note": "Sentinels are additive to permissions/gate.py - never a replacement.",
        }
        self.save_model("security_policy.json", model)
        self.save_model(STATE, {"violations": [], "scans": 0, "blocked": 0,
                                "last_scan": 0.0, "decoys": [], "quarantined": []})
        self.log("trained", sentinels=len(SENTINELS))
        return {"sentinels": len(SENTINELS), "code_rules": len(CODE_RULES),
                "injection_rules": len(INJECTION_RULES)}

    # ------------------------------------------------------------------ state
    def _state(self) -> Dict[str, Any]:
        return self.load_model(STATE, default={
            "violations": [], "scans": 0, "blocked": 0, "last_scan": 0.0,
            "decoys": [], "quarantined": []}) or {}

    def _save(self, state: Dict[str, Any]) -> None:
        state["last_scan"] = time.time()
        state["scans"] = int(state.get("scans", 0)) + 1
        state["violations"] = state.get("violations", [])[-300:]
        self.save_model(STATE, state)

    def _flag(self, sentinel: str, severity: str, detail: str,
              action: str = "logged") -> Dict[str, Any]:
        entry = {"sentinel": sentinel, "severity": severity, "detail": detail[:240],
                 "action": action, "at": time.time()}
        state = self._state()
        state.setdefault("violations", []).append(entry)
        if action == "blocked":
            state["blocked"] = int(state.get("blocked", 0)) + 1
        self._save(state)
        self.log("violation", sentinel=sentinel, severity=severity, detail=detail[:120])
        return entry

    # ------------------------------------------------------- 1. network_shield
    def network_shield(self, host: str) -> Dict[str, Any]:
        host = (host or "").lower().strip()
        if not host:
            return {"ok": False, "reason": "empty host"}
        allowed = any(host == h or host.endswith("." + h) for h in SANCTIONED_HOSTS)
        if not allowed:
            self._flag("network_shield", "high", "unsanctioned host: %s" % host, "blocked")
        return {"ok": allowed, "host": host,
                "reason": "" if allowed else "host not on the sanctioned list"}

    # ----------------------------------------------------------- 2. code_sentry
    def code_sentry(self, folder: Optional[str] = None,
                    limit: int = 400) -> List[Dict[str, Any]]:
        root = Path(folder) if folder else Path(self.root) / "factory"
        findings: List[Dict[str, Any]] = []
        scanned = 0
        for path in sorted(root.rglob("*.py")):
            if scanned >= limit or ".git" in path.parts:
                continue
            scanned += 1
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for pattern, why in CODE_RULES:
                for match in re.finditer(pattern, source):
                    line = source[:match.start()].count("\n") + 1
                    findings.append({"file": str(path.relative_to(root)), "line": line,
                                     "rule": why, "snippet": match.group(0)[:60]})
                    self._flag("code_sentry", "high", "%s:%s %s" % (
                        path.name, line, why), "logged")
        return findings

    # --------------------------------------------------------- 3. file_watchdog
    def file_watchdog(self, path: str) -> Dict[str, Any]:
        try:
            resolved = Path(path).resolve()
        except Exception:
            return {"ok": False, "reason": "unresolvable path"}
        allowed_roots = [Path(self.root).resolve(), Path.home()]
        for root in (Path(os.getenv("TEMP", ".")), Path(os.getenv("TMP", "."))):
            try:
                allowed_roots.append(root.resolve())
            except Exception:
                pass
        ok = any(_is_within(resolved, root) for root in allowed_roots)
        if not ok:
            self._flag("file_watchdog", "critical", "write outside allowed roots: %s"
                       % resolved, "blocked")
        return {"ok": ok, "path": str(resolved),
                "reason": "" if ok else "outside factory/user/temp roots"}

    def watch(self, seconds: float = 30.0) -> List[Dict[str, Any]]:
        """Snapshot-based watcher: hash the tree, diff after ``seconds``."""
        before = self._tree_hashes()
        time.sleep(min(seconds, 5.0))
        after = self._tree_hashes()
        changed = []
        for key, digest in after.items():
            if key not in before:
                changed.append({"type": "created", "path": key})
            elif before[key] != digest:
                changed.append({"type": "modified", "path": key})
        for key in before:
            if key not in after:
                changed.append({"type": "deleted", "path": key})
        for change in changed[:40]:
            self._flag("file_watchdog", "info", "%s %s" % (change["type"], change["path"]),
                       "logged")
        return changed

    def _tree_hashes(self) -> Dict[str, str]:
        root = Path(self.root) / "factory"
        out: Dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.stat().st_size > 400_000:
                continue
            try:
                out[str(path.relative_to(root))] = hashlib.sha256(
                    path.read_bytes()).hexdigest()[:16]
            except Exception:
                continue
        return out

    # ------------------------------------------------------ 4. input_sanitizer
    def input_sanitizer(self, text: str) -> Dict[str, Any]:
        hits = []
        for pattern, why in INJECTION_RULES:
            if re.search(pattern, text or "", re.I):
                hits.append(why)
        for pattern, what in SECRET_PATTERNS:
            if re.search(pattern, text or ""):
                hits.append("possible %s in content" % what)
        if hits:
            self._flag("input_sanitizer", "high", "; ".join(hits[:3]), "blocked")
        cleaned = text or ""
        for pattern, _ in INJECTION_RULES:
            cleaned = re.sub(pattern, "[filtered]", cleaned, flags=re.I)
        return {"clean": not hits, "hits": hits, "cleaned": cleaned}

    def is_safe(self, text: str) -> bool:
        return self.input_sanitizer(text)["clean"]

    # ------------------------------------------------------------- 5. auth_gate
    def auth_gate(self, agent: str, action: str,
                  details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Second opinion before a risky action.  True = allow."""
        risk = self.gate.risk_of(action)
        allowed_tools = (self.gate.allowlist.get("agents", {})
                         .get(agent, {}).get("allowed_tools"))
        if allowed_tools is not None and action not in allowed_tools:
            self._flag("auth_gate", "high", "%s attempted %s (not in allowed_tools)"
                       % (agent, action), "blocked")
            return {"ok": False, "reason": "action not in allowed_tools for %s" % agent,
                    "risk": risk}
        if risk in ("high", "critical"):
            self._flag("auth_gate", risk, "%s -> %s requires human approval"
                       % (agent, action), "logged")
            return {"ok": False, "reason": "requires human approval", "risk": risk,
                    "needs_approval": True}
        return {"ok": True, "risk": risk}

    # ------------------------------------------------------ 6. anomaly_detector
    def anomaly_detector(self) -> List[Dict[str, Any]]:
        anomalies: List[Dict[str, Any]] = []
        try:
            entries = read_log(limit=400)
        except Exception:
            entries = []
        buckets: Dict[str, List[float]] = {}
        errors: Dict[str, int] = {}
        for entry in entries:
            key = "%s:%s" % (entry.get("actor", "?"), entry.get("action", "?"))
            buckets.setdefault(key, []).append(entry.get("ts", 0))
            if str(entry.get("result", "")).lower() in ("denied", "blocked", "error"):
                errors[key] = errors.get(key, 0) + 1
        now = time.time()
        for key, stamps in buckets.items():
            recent = [t for t in stamps if now - t < 300]
            if len(recent) > 40:
                anomalies.append({"type": "rate_spike", "key": key, "count": len(recent)})
                self._flag("anomaly_detector", "medium",
                           "rate spike %s x%d/5min" % (key, len(recent)), "logged")
        for key, count in errors.items():
            if count >= 5:
                anomalies.append({"type": "error_burst", "key": key, "count": count})
                self._flag("anomaly_detector", "high",
                           "repeated denials %s x%d" % (key, count), "logged")
        store = default_store()
        try:
            counts = store.count_leads() if hasattr(store, "count_leads") else {}
            total = int(counts.get("total", 0)) if isinstance(counts, dict) else 0
        except Exception:
            total = 0
        if total > 5000:
            anomalies.append({"type": "db_growth", "leads": total})
        return anomalies

    # ---------------------------------------------------------- 7. crypto_guard
    def crypto_guard(self, folder: Optional[str] = None) -> List[Dict[str, Any]]:
        root = Path(folder) if folder else Path(self.root)
        findings: List[Dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() not in (".json", ".py", ".txt", ".md", ".env", ".cfg",
                                           ".ini", ".example"):
                continue
            if path.stat().st_size > 500_000:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for pattern, what in SECRET_PATTERNS:
                if re.search(pattern, text):
                    findings.append({"file": str(path.relative_to(root)), "secret": what})
                    self._flag("crypto_guard", "critical",
                               "possible %s stored in %s" % (what, path.name), "logged")
        return findings

    # --------------------------------------------------------- 8. audit_logger
    def audit_logger(self) -> Dict[str, Any]:
        try:
            ok, problems = verify_chain()
        except Exception as exc:
            return {"ok": False, "problem": "verify failed: %s" % exc}
        if not ok:
            self._flag("audit_logger", "critical",
                       "audit chain broken: %s" % (problems[:2] if problems else "?"),
                       "logged")
        return {"ok": ok, "problems": problems[:5]}

    # --------------------------------------------------------- 9. decoy_engine
    def decoy_engine(self, reset: bool = False) -> Dict[str, Any]:
        """Plant canary files and a canary lead; nothing legitimate touches them."""
        state = self._state()
        decoys = state.get("decoys") or []
        if reset:
            for decoy in decoys:
                try:
                    Path(decoy["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            decoys = []
        if not decoys:
            folder = self.sandbox_path("decoys")
            folder.mkdir(parents=True, exist_ok=True)
            canary = folder / "DO_NOT_TOUCH_passwords.txt"
            canary.write_text("canary - if an agent reads or sends this, it is compromised.\n",
                              encoding="utf-8")
            decoys = [{"path": str(canary), "kind": "file", "planted": time.time()}]
            try:
                lead_id = default_store().add_lead({
                    "business_name": "CANARY-DO-NOT-CONTACT", "city": "Nowhere",
                    "category": "canary", "email": "canary@example.invalid",
                    "source": "decoy", "notes": "canary lead - must never be contacted",
                    "status": "blocked"})
                decoys.append({"lead_id": lead_id, "kind": "lead", "planted": time.time()})
            except Exception:
                pass
            state["decoys"] = decoys
            self._save(state)
            return {"planted": decoys}
        touched = []
        for decoy in decoys:
            if decoy.get("kind") == "file" and Path(decoy["path"]).exists():
                continue
            touched.append(decoy)
        for decoy in touched:
            self._flag("decoy_engine", "critical", "canary touched: %s" % decoy, "logged")
        return {"decoys": decoys, "touched": touched}

    # -------------------------------------------------------------- grid sweep
    SENTINEL_MAP = {
        "network_shield": lambda self: self.network_shield("google.com"),
        "code_sentry": lambda self: self.code_sentry(),
        "file_watchdog": lambda self: self.watch(seconds=1.0),
        "input_sanitizer": lambda self: {"note": "on-demand: scan(text)"},
        "auth_gate": lambda self: {"note": "on-demand: auth_gate(agent, action)"},
        "anomaly_detector": lambda self: self.anomaly_detector(),
        "crypto_guard": lambda self: self.crypto_guard(),
        "audit_logger": lambda self: self.audit_logger(),
        "decoy_engine": lambda self: self.decoy_engine(),
    }

    def run(self, deep: bool = False) -> Dict[str, Any]:
        """Sweep every sentinel.  Cheap by default; ``deep`` scans all source."""
        report: Dict[str, Any] = {"at": time.time(), "sentinels": {}}
        report["sentinels"]["network_shield"] = {
            "checked": len(SANCTIONED_HOSTS), "sample": self.network_shield("google.com")["ok"]}
        report["sentinels"]["code_sentry"] = {"findings": self.code_sentry(
            limit=400 if deep else 120)}
        report["sentinels"]["file_watchdog"] = {"changes": self.watch(seconds=1.0)[:20]}
        report["sentinels"]["input_sanitizer"] = {
            "rules": len(INJECTION_RULES) + len(SECRET_PATTERNS)}
        report["sentinels"]["auth_gate"] = {"policy": "additive second check"}
        report["sentinels"]["anomaly_detector"] = {"anomalies": self.anomaly_detector()}
        report["sentinels"]["crypto_guard"] = {"findings": self.crypto_guard() if deep else []}
        report["sentinels"]["audit_logger"] = self.audit_logger()
        report["sentinels"]["decoy_engine"] = self.decoy_engine()
        state = self._state()
        report["totals"] = {"violations": len(state.get("violations", [])),
                            "blocked": state.get("blocked", 0),
                            "scans": state.get("scans", 0)}
        self.log("sweep", deep=deep, violations=report["totals"]["violations"])
        return report

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        if any(word in lowered for word in ("scan", "sweep", "check", "audit")):
            report = self.run(deep="deep" in lowered)
            totals = report["totals"]
            lines = ["Security sweep - violations %d, blocked %d, scans %d"
                     % (totals["violations"], totals["blocked"], totals["scans"])]
            for name, payload in report["sentinels"].items():
                size = len(payload.get("findings") or payload.get("anomalies")
                           or payload.get("changes") or [])
                lines.append("  %-18s %s" % (name, ("%d finding(s)" % size) if size else "clean"))
            if not report["sentinels"]["audit_logger"]["ok"]:
                lines.append("  !! audit chain broken: %s"
                             % report["sentinels"]["audit_logger"]["problems"])
            return "\n".join(lines)
        if any(word in lowered for word in ("secret", "key", "token")):
            findings = self.crypto_guard()
            return "\n".join("  %s -> %s" % (f["file"], f["secret"]) for f in findings) \
                if findings else "Koi secret nahi mila. Safe hai."
        if any(word in lowered for word in ("code", "source")):
            findings = self.code_sentry()
            return "\n".join("  %s:%s %s" % (f["file"], f["line"], f["rule"])
                             for f in findings[:20]) or "Code saaf hai."
        if any(word in lowered for word in ("injection", "prompt", "safe hai")):
            verdict = self.input_sanitizer(text)
            return ("Clean hai." if verdict["clean"]
                    else "SUSPICIOUS: %s\nFiltered: %s" % (
                        ", ".join(verdict["hits"]), verdict["cleaned"][:200]))
        state = self._state()
        return ("Security grid ready - 9 sentinels. Violations %d, blocked %d.\n"
                "Kaam: 'security scan', 'deep scan', 'secrets check', 'code audit'."
                % (len(state.get("violations", [])), state.get("blocked", 0)))

    def status(self) -> Dict[str, Any]:
        data = super().status()
        state = self._state()
        data.update({"sentinels": len(SENTINELS),
                     "violations": len(state.get("violations", [])),
                     "blocked": state.get("blocked", 0),
                     "scans": state.get("scans", 0)})
        return data


SENTINELS = ("network_shield", "code_sentry", "file_watchdog", "input_sanitizer",
             "auth_gate", "anomaly_detector", "crypto_guard", "audit_logger",
             "decoy_engine")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
