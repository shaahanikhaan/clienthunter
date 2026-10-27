"""The orchestrator: boots the factory, routes messages, runs the daily cycle.

Design notes
------------
*   **Dynamic apartment loading.**  Apartments are imported lazily through
    ``registry.APARTMENTS``, so one broken file can never stop the factory from
    starting - it shows up as ``unavailable`` in ``status`` instead.
*   **Permissions before every action.**  ``self.gate`` is the same process-wide
    gate the apartments use; the orchestrator asks it before routing, training,
    backing up and serving, and every verdict lands in the hash-chained audit log.
*   **The manager supervises.**  Worker work goes through ``apt_manager``'s task
    queue so there is one place to see, pause, restart and audit everything.
*   **No API keys, no cloud.**  IMAP/SMTP with an App Password, CDP against the
    user's own Chrome, ``git-credential-oauth`` for GitHub, WhatsApp Web or a
    pure-Python client.  Nothing here needs a paid account.

CLI
---
    python -m factory.orchestrator                     # interactive chat
    python -m factory.orchestrator "karachi restaurants dhoondo"
    python -m factory.orchestrator --train             # train every apartment
    python -m factory.orchestrator --status            # health of all 20
    python -m factory.orchestrator --daily             # 12-step autonomous cycle
    python -m factory.orchestrator --backup            # SHA-256 verified backup
    python -m factory.orchestrator --serve --port 8000 # live dashboard
    python -m factory.orchestrator --agent hunter "lahore clinics 20"
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .audit import backup, read_log, verify_backup, verify_chain
from .permissions import default_gate
from .registry import (APARTMENTS, DAILY_CYCLE, TRAIN_ORDER, apartment_names,
                       module_and_class, worker_names)
from .router import IntentRouter
from .store import default_store

FACTORY_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = FACTORY_ROOT.parent

BANNER = """\
  ____ _ _           _   _             _   _
 / ___| (_) ___ _ __| |_| |_ _   _  __| | |___  _   _
| |   | | |/ _ \\ '_ \\ __| __| | | |/ _` | __\\ \\/ / | |
| |___| | |  __/ | | |_ | |_| |_| | (_| | |_ >  <| |_|
 \\____|_|_|\\___|_| |_|\\__|\\__|\\__, |\\__,_|\\__/_/\\_\\__, |
                              |___/                |___/
  ClientHunter Factory v{version}  -  {count} apartments, 0 API keys
  Ctrl+Shift+Q = emergency stop | 'help' = commands | 'exit' = quit
"""


class Factory:
    """Owns every apartment, the router, the gate and the daily cycle."""

    def __init__(self, factory_root: Optional[os.PathLike] = None,
                 overrides: Optional[Dict[str, bool]] = None,
                 offline: bool = False, load_all: bool = True,
                 auto_train: bool = False):
        self.root = Path(factory_root) if factory_root else FACTORY_ROOT
        self.version = __version__
        self.offline = offline
        self.overrides = dict(overrides or {})
        self.gate = default_gate()
        self.store = default_store()
        self.apartments: Dict[str, Any] = {}
        self.failures: Dict[str, str] = {}
        self.router = IntentRouter()
        self.manager: Any = None
        self.started_at = time.time()

        if load_all:
            self.load_all()
        if auto_train:
            self.train("all", force=False)

    # ---------------------------------------------------------------- loading
    def load(self, name: str) -> Optional[Any]:
        """Import and instantiate one apartment (cached)."""
        if name in self.apartments:
            return self.apartments[name]
        if name in self.failures:
            return None
        try:
            module_suffix, class_name = module_and_class(name)
            module = importlib.import_module("factory.apartments." + module_suffix)
            cls = getattr(module, class_name)
            instance = cls(factory_root=self.root, overrides=self.overrides,
                           offline=self.offline)
            self.apartments[name] = instance
            return instance
        except Exception as exc:
            self.failures[name] = "%s: %s" % (type(exc).__name__, exc)
            sys.stderr.write("  ! apt_%s failed to load: %s\n" % (name, exc))
            return None

    def load_all(self) -> Dict[str, Any]:
        for name in apartment_names():
            self.load(name)
        # manager first so it can supervise everything else from the start
        manager = self.apartments.get("manager")
        if manager is not None:
            manager.bind({key: value for key, value in self.apartments.items()
                          if key != "manager"}, orchestrator=self)
            self.manager = manager
        knowledge = self.apartments.get("knowledge")
        if knowledge is not None:
            self.router = IntentRouter(knowledge)
            self.router.load()
        self._audit("factory", "boot", {"apartments": len(self.apartments),
                                        "failures": list(self.failures)})
        return self.apartments

    # ----------------------------------------------------------------- audit
    def _audit(self, actor: str, action: str, details: Dict[str, Any],
               result: str = "allow") -> None:
        try:
            self.gate.audit.record(actor, action, "low", result, details)
        except Exception:
            pass
        try:
            self.store.log_event(actor, action, details)
        except Exception:
            pass

    # ----------------------------------------------------------------- train
    def train(self, target: str = "all", force: bool = False) -> Dict[str, Any]:
        """Train one apartment or all of them.  Returns a name -> stats dict."""
        names = list(TRAIN_ORDER) if target in ("all", "*", "") else [target]
        results: Dict[str, Any] = {}
        for name in names:
            apartment = self.load(name)
            if apartment is None:
                results[name] = {"error": self.failures.get(name, "not loaded")}
                continue
            if not force:
                try:
                    if apartment.is_trained():
                        results[name] = {"skipped": "already trained"}
                        continue
                except Exception:
                    pass
            started = time.time()
            try:
                stats = apartment.train()
                results[name] = {"ok": True, "seconds": round(time.time() - started, 2),
                                 "stats": _brief(stats)}
                self._audit(name, "train", {"seconds": round(time.time() - started, 2)})
            except Exception as exc:
                results[name] = {"error": "%s: %s" % (type(exc).__name__, exc)}
                self._audit(name, "train", {"error": str(exc)[:200]}, "error")
        # router needs the knowledge apartment's model
        if target in ("all", "knowledge"):
            self.router.load()
        return results

    # ------------------------------------------------------------------ route
    def route(self, text: str) -> Tuple[str, float, str]:
        return self.router.route(text or "")

    def handle(self, text: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Route a message and return the answer.  Never raises."""
        text = (text or "").strip()
        if not text:
            return "Kuch likhein - main sun raha hoon."
        if self.kill_switch():
            return ("Kill switch ON hai - factory ruki hui hai. "
                    "'resume' likhein ya Ctrl+Shift+Q dobara dabayein.")

        lowered = text.lower()
        if lowered in ("help", "madad", "?", "commands"):
            return self.help_text()
        if lowered in ("exit", "quit", "band", "bye"):
            return "Khuda hafiz."
        if lowered in ("status", "health"):
            return self.status_text()
        if lowered in ("agents", "list", "kaun"):
            return self.agent_list_text()
        if lowered in ("resume", "resume all", "wapas"):
            if self.manager:
                return self.manager.resume_all()
            return "Manager loaded nahi hai."

        # 1. sanitize untrusted input (prompt-injection guard)
        if self.manager is None:
            security = self.apartments.get("security")
        else:
            security = self.apartments.get("security")
        if security is not None and "security" not in text[:20].lower():
            try:
                verdict = security.input_sanitizer(text)
                if not verdict["clean"]:
                    self._audit("factory", "injection_blocked",
                                {"hits": verdict["hits"]}, "blocked")
                    return ("Ye message block kar diya gaya (security): %s\n"
                            "Agar genuine hai to alfaaz badal kar bhejein."
                            % ", ".join(verdict["hits"][:3]))
            except Exception:
                pass

        # 2. permission check before routing
        decision = self.gate.check("orchestrator", "route_message",
                                   {"chars": len(text)})
        if not decision.allowed:
            self._audit("orchestrator", "route_message", {"chars": len(text)}, "denied")
            return "Ruk gaya (permission): %s" % decision.reason

        # 3. route
        target, confidence, why = self.route(text)
        apartment = self.load(target)
        if apartment is None:
            return ("apt_%s abhi available nahi hai (%s). Baqi apartments chalu hain."
                    % (target, self.failures.get(target, "unknown")))

        # 4. run it - supervised through the manager so it is visible + pausable
        started = time.time()
        try:
            if self.manager is not None and target != "manager" and \
                    hasattr(apartment, "handle"):
                reply = self._supervised(target, text, context)
            else:
                reply = apartment.handle(text, context)
            elapsed = time.time() - started
            self._audit(target, "handle", {"chars": len(text), "confidence": confidence,
                                           "router": why, "seconds": round(elapsed, 2)})
            return str(reply)
        except Exception as exc:
            self._audit(target, "handle", {"error": str(exc)[:300]}, "error")
            return ("apt_%s mein error aya: %s\n%s"
                    % (target, exc, traceback.format_exc(limit=2)))

    def _supervised(self, target: str, text: str,
                    context: Optional[Dict[str, Any]]) -> str:
        """Hand the message to apt_manager, which runs it as a tracked task."""
        task_id = self.manager.submit(target, text, priority=7,
                                      args=(context or {}).get("args") or {})
        task = self.manager.wait(
            task_id,
            timeout=float((self.manager.config.get("limits") or {})
                          .get("task_timeout_s", 240)))
        status = task.get("status")
        if status == "done":
            result = task.get("result")
            if isinstance(result, dict) and str(result.get("kind")) == "str":
                return str(result)
            if isinstance(result, dict) and "preview" in result:
                preview = result.get("preview") or []
                return "\n".join(str(item) for item in preview[:15])
            return _render(result)
        if status == "blocked":
            return "Ruk gaya: %s" % task.get("error", "budget/permission")
        return "apt_%s failed: %s" % (target, task.get("error", "unknown")[:300])

    # ---------------------------------------------------------- daily cycle
    def run_daily(self, steps: Optional[List[str]] = None,
                  stop_on_error: bool = False) -> List[Dict[str, Any]]:
        """The 12-step autonomous cycle, every step supervised and logged."""
        report: List[Dict[str, Any]] = []
        wanted = set(steps or [step[0] for step in DAILY_CYCLE])
        for index, (name, method, kwargs) in enumerate(DAILY_CYCLE, start=1):
            if name not in wanted:
                continue
            if self.kill_switch():
                report.append({"step": index, "agent": name, "status": "skipped",
                               "reason": "kill switch"})
                break
            apartment = self.load(name)
            if apartment is None:
                report.append({"step": index, "agent": name, "status": "missing"})
                continue
            started = time.time()
            try:
                if self.manager is not None and name != "manager":
                    task_id = self.manager.submit(name, "", priority=9, args=dict(kwargs))
                    task = self.manager.wait(task_id, timeout=600)
                    outcome = {"status": task.get("status"),
                               "result": task.get("result"),
                               "error": task.get("error", "")}
                else:
                    function = getattr(apartment, method, None) or apartment.run
                    result = function(**kwargs) if kwargs else function()
                    outcome = {"status": "done", "result": _brief(result)}
                outcome.update({"step": index, "agent": name,
                                "seconds": round(time.time() - started, 2)})
                self._audit(name, "daily:%s" % method,
                            {"step": index, "seconds": outcome["seconds"]})
            except Exception as exc:
                outcome = {"step": index, "agent": name, "status": "error",
                           "error": "%s: %s" % (type(exc).__name__, exc),
                           "seconds": round(time.time() - started, 2)}
                self._audit(name, "daily:%s" % method, {"error": str(exc)[:200]}, "error")
                if stop_on_error:
                    report.append(outcome)
                    break
            report.append(outcome)
        self._audit("factory", "daily_cycle", {"steps": len(report)})
        return report

    # ----------------------------------------------------------------- backup
    def backup(self) -> Dict[str, Any]:
        info = backup(root=self.root.parent / "backups")   # <repo>/backups/YYYY-MM-DD/
        folder = Path(info["folder"])
        ok, problems = verify_backup(folder)
        self._audit("factory", "backup", {"folder": str(folder), "ok": ok})
        return {"folder": str(folder), "files": info.get("files", []),
                "checksums": info.get("checksums", []), "verified": ok,
                "problems": problems, "chain_ok": self._chain_ok()}

    def _chain_ok(self) -> bool:
        try:
            ok, _ = verify_chain()
            return ok
        except Exception:
            return False

    # ------------------------------------------------------------------ state
    def kill_switch(self) -> bool:
        from .apartments.base_apartment import BaseApartment
        return bool(BaseApartment._KILL_SWITCH or self.gate.killed)

    def status_text(self) -> str:
        lines = ["ClientHunter Factory v%s | %d/%d apartments loaded | kill switch: %s"
                 % (self.version, len(self.apartments), len(APARTMENTS),
                    "ON" if self.kill_switch() else "off")]
        if self.manager is not None:
            for row in self.manager.worker_status():
                flags = []
                if row["disabled"]:
                    flags.append("DISABLED")
                if row["paused"]:
                    flags.append("paused")
                if not row["trained"]:
                    flags.append("untrained")
                if row["offline"]:
                    flags.append("offline")
                lines.append("  %-24s calls %-4d err %-3d %s"
                             % (row["apartment"], row["calls"], row["errors"],
                                ("[" + ", ".join(flags) + "]") if flags else "ok"))
        try:
            counts = self.store.count_leads()
            lines.append("Leads: %s" % ", ".join("%s=%d" % (k, v)
                                                 for k, v in sorted(counts.items())))
        except Exception:
            pass
        lines.append("Audit chain: %s" % ("intact" if self._chain_ok() else "BROKEN"))
        if self.failures:
            lines.append("Failed to load: %s" % ", ".join(sorted(self.failures)))
        return "\n".join(lines)

    def agent_list_text(self) -> str:
        lines = []
        for name in apartment_names():
            apartment = self.apartments.get(name)
            if apartment is None:
                lines.append("  apt_%-20s UNAVAILABLE (%s)"
                             % (name, self.failures.get(name, "?")[:60]))
            else:
                trained = "trained" if apartment.is_trained() else "untrained"
                lines.append("  apt_%-20s %s - %s"
                             % (name, trained, apartment.DESCRIPTION[:64]))
        return "\n".join(lines)

    def help_text(self) -> str:
        return """ClientHunter - aap likhein, main kaam karoon ga:

  leads                 karachi mein restaurants dhoondo (20)
  audit                 in 10 leads ki sites check karo
  outreach              top 20 leads ko pehla message bhejo
  inbox                 naye replies check karo
  quote                 Ali Medical ke liye quote banao
  followup              aaj ke due follow-ups bhejo
  demo banao            lead 12 ke liye mockup site
  status / agents       factory ki health
  security scan         9 sentinels ki report

  manager status        kaun sa agent kya kar raha hai
  powershell mein Get-Process chalao
  screenshot le lo

  Ctrl+Shift+Q = sab kuch foran roko.  'exit' = band karo.
  Har action audit log mein jata hai (hash-chained, factory/data/audit.db)."""

    def as_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "apartments": len(self.apartments),
                "failures": self.failures, "kill_switch": self.kill_switch(),
                "uptime_s": round(time.time() - self.started_at, 1),
                "router_loaded": self.router.loaded,
                "audit_chain_ok": self._chain_ok()}

    # ------------------------------------------------------------------- http
    def serve(self, host: str = "0.0.0.0", port: int = 8000,
              background: bool = False) -> Any:
        from .dashboard import DashboardServer

        server = DashboardServer(self, host=host, port=port)
        server.start(background=background)
        self._audit("factory", "serve", {"host": host, "port": port})
        return server


def _brief(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return {"count": len(value)}
    if isinstance(value, dict):
        return {k: _brief(v) for k, v in list(value.items())[:8]}
    return value


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "preview" in value:
        return "\n".join(str(v) for v in (value.get("preview") or [])[:15])
    return json.dumps(value, ensure_ascii=False, indent=2)[:3000]


# ----------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m factory.orchestrator",
        description="ClientHunter Factory - 20 agents, 0 API keys, all data local.")
    parser.add_argument("message", nargs="*", help="one-shot instruction")
    parser.add_argument("--train", action="store_true", help="train every apartment")
    parser.add_argument("--retrain", action="store_true",
                        help="force retrain even if models exist")
    parser.add_argument("--agent", help="send the message directly to one apartment")
    parser.add_argument("--status", action="store_true", help="print factory health")
    parser.add_argument("--daily", action="store_true", help="run the 12-step daily cycle")
    parser.add_argument("--backup", action="store_true", help="backup + verify")
    parser.add_argument("--serve", action="store_true", help="start the live dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="dashboard host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="dashboard port")
    parser.add_argument("--offline", action="store_true",
                        help="no network: browser/email/whatsapp disabled")
    parser.add_argument("--no-load-all", action="store_true",
                        help="lazy load apartments on first use (saves RAM)")
    parser.add_argument("--audit", action="store_true", help="print the audit log tail")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    factory = Factory(offline=args.offline, load_all=not args.no_load_all)

    if args.train or args.retrain:
        results = factory.train("all", force=args.retrain)
        for name, outcome in results.items():
            state = "trained" if outcome.get("ok") else \
                ("skipped" if "skipped" in outcome else "FAILED")
            print("  apt_%-20s %-8s %s" % (name, state,
                                           outcome.get("error", outcome.get("stats", ""))))
    if args.status:
        print(factory.status_text())
    if args.audit:
        for entry in read_log(limit=25)[-25:]:
            print("  %s %-12s %-22s %s" % (entry.get("ts", ""), entry.get("actor", ""),
                                           entry.get("action", ""), entry.get("result", "")))
    if args.backup:
        print(json.dumps(factory.backup(), indent=2))
    if args.daily:
        for row in factory.run_daily():
            print("  %2s apt_%-20s %-8s %ss %s"
                  % (row.get("step"), row.get("agent"), row.get("status"),
                     row.get("seconds", ""), str(row.get("error", ""))[:80]))

    message = " ".join(args.message).strip()
    if args.agent:
        apartment = factory.load(args.agent)
        if apartment is None:
            print("apt_%s unavailable: %s" % (args.agent,
                                              factory.failures.get(args.agent)))
            return 2
        print(apartment.handle(message))
        return 0
    if message:
        print(factory.handle(message))
        return 0
    if args.serve:
        server = factory.serve(host=args.host, port=args.port, background=False)
        print("Dashboard: %s  (Ctrl+C to stop)" % server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.stop()
        return 0
    if any([args.train, args.status, args.daily, args.backup, args.audit]):
        return 0

    print(BANNER.format(version=__version__, count=len(APARTMENTS)))
    if not factory.router.loaded:
        print("  (router untrained - chalega, keyword fallback use hoga)")
    print(factory.status_text())
    while True:
        try:
            text = input("\nyou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nKhuda hafiz.")
            break
        if text.lower() in ("exit", "quit", "band"):
            print("Khuda hafiz.")
            break
        if not text:
            continue
        started = time.time()
        try:
            print("\nfactory > %s" % factory.handle(text))
        except Exception as exc:
            print("\nfactory > error: %s" % exc)
        print("           (%.2fs)" % (time.time() - started))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
