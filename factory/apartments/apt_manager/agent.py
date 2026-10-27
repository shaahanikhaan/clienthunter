"""apt_manager - the supervisor that actually runs the factory.

Topology: **hierarchical supervisor** - the pattern that holds up at 10-50
agents, where a single manager decomposes goals, delegates to scoped workers,
and collects results [4](https://www.paiteq.com/blog/multi-agent-orchestration-patterns/).
Workers never talk to each other; everything flows through here, which is what
makes the whole thing debuggable and killable.

What the manager owns:

*   **Task queue** - priority, concurrency cap (RAM-aware on a 4 GB box),
    retries, per-task timeouts, full result capture.
*   **Worker lifecycle** - lazy import, pause/resume/restart/disable,
    heartbeat monitoring, auto-quarantine after repeated failures.
*   **Budgets** - per-worker daily call limits, so one chatty agent cannot eat
    the whole day's WhatsApp/email quota.
*   **Real machine access** - terminal (CMD/PowerShell/bash, allowlist-gated),
    mouse/keyboard, screen capture.  Same access you have, no more.
*   **Emergency control** - kill switch, quarantine, resume.

It also learns routing: which worker succeeded at which task type is tracked in
``models/routing_stats.json`` and biases future delegation.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...store import default_store
from ..base_apartment import BaseApartment

#: Task type -> (worker key, method name, fallback to handle()).
TASK_ROUTES: Dict[str, Tuple[str, str]] = {
    "hunt": ("hunter", "find_leads"),
    "find_leads": ("hunter", "find_leads"),
    "enrich": ("enricher", "enrich_batch"),
    "audit": ("auditor", "audit_batch"),
    "outreach": ("outreach", "run_batch"),
    "contact": ("outreach", "run_batch"),
    "inbox": ("inbox", "process_replies"),
    "replies": ("inbox", "process_replies"),
    "requirements": ("requirements", "run"),
    "convince": ("convincer", "handle"),
    "quote": ("closer", "run"),
    "close": ("closer", "run"),
    "followup": ("followup", "send_due"),
    "build": ("builder", "build"),
    "demo": ("builder", "build"),
    "knowledge": ("knowledge", "handle"),
    "analyse": ("urdu_nlp", "analyse"),
    "whatsapp": ("platform_whatsapp", "run"),
    "linkedin": ("platform_linkedin", "run"),
    "facebook": ("platform_facebook", "run"),
    "instagram": ("platform_instagram", "run"),
    "github": ("platform_github", "run"),
    "train": ("self_trainer", "retrain_all"),
    "security": ("security", "run"),
    "terminal": ("manager", "run_command"),
}

#: Worker -> (method name, how to build its arguments).  Used whenever the
#: router hands us an apartment name rather than an explicit task type.
DEFAULT_METHOD: Dict[str, Tuple[str, str]] = {
    "hunter": ("find_leads", "city"),
    "enricher": ("enrich_batch", "limit"),
    "auditor": ("audit_batch", "limit"),
    "outreach": ("run_batch", "limit"),
    "inbox": ("process_replies", "limit"),
    "urdu_nlp": ("analyse", "text"),
    "requirements": ("handle", "text"),
    "convincer": ("handle", "text"),
    "closer": ("handle", "text"),
    "followup": ("send_due", "limit"),
    "builder": ("build", "lead"),
    "knowledge": ("handle", "text"),
    "platform_whatsapp": ("run", "limit"),
    "platform_linkedin": ("run", "limit"),
    "platform_facebook": ("run", "limit"),
    "platform_instagram": ("run", "limit"),
    "platform_github": ("run", "none"),
    "self_trainer": ("learn", "none"),
    "security": ("run", "none"),
    "manager": ("run_command", "text"),
}

#: Which apartments are workers (everything except the manager itself).
WORKER_ROLES = ("hunter", "enricher", "auditor", "outreach", "inbox", "urdu_nlp",
                "requirements", "convincer", "closer", "followup", "builder",
                "knowledge", "platform_whatsapp", "platform_linkedin",
                "platform_facebook", "platform_instagram", "platform_github",
                "self_trainer", "security")

TERMINAL = "terminal"


class ManagerAgent(BaseApartment):
    NAME = "manager"
    DESCRIPTION = "Supervisor: task queue, worker lifecycle, budgets, terminal/mouse/screen control"
    PERMISSIONS = {"subprocess": True, "internet": False, "email": False,
                   "whatsapp": False, "browser": True}

    MODEL = "routing_stats.json"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workers: Dict[str, Any] = {}            # key -> apartment instance
        self.worker_state: Dict[str, Dict[str, Any]] = {}
        self.tasks: Dict[int, Dict[str, Any]] = {}
        self._task_seq = 0
        self._lock = threading.RLock()
        self._paused: set = set()
        self._disabled: set = set()
        self._executor = ThreadPoolExecutor(max_workers=int(
            self.config.get("limits", {}).get("max_concurrent_workers", 3)),
            thread_name_prefix="mgr")
        self._budgets: Dict[str, Dict[str, int]] = {}
        self._events: List[Dict[str, Any]] = []
        self._max_events = 400
        self._orchestrator = None

    # ------------------------------------------------------------------ setup
    def bind(self, apartments: Dict[str, Any], orchestrator: Any = None) -> None:
        """Attach the live worker instances (the orchestrator loads them)."""
        self.workers = {key: value for key, value in apartments.items()
                        if key in WORKER_ROLES}
        self._orchestrator = orchestrator
        for key in self.workers:
            self.worker_state.setdefault(key, {
                "calls": 0, "errors": 0, "last_call": 0.0, "last_error": "",
                "heartbeat": time.time(), "consecutive_errors": 0})
        self.log("bound", workers=len(self.workers))

    def _state(self, key: str) -> Dict[str, Any]:
        return self.worker_state.setdefault(key, {
            "calls": 0, "errors": 0, "last_call": 0.0, "last_error": "",
            "heartbeat": time.time(), "consecutive_errors": 0})

    def _emit(self, kind: str, **payload) -> None:
        event = {"kind": kind, "at": time.time(), **payload}
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events:]
        self.log(kind, **{k: (str(v)[:120] if isinstance(v, (dict, list)) else v)
                          for k, v in payload.items() if k != "result"})

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        learned = self.load_model(self.MODEL, default={}) or {}
        model = {
            "routes": TASK_ROUTES,
            "success": learned.get("success", {}),     # task_type -> [ok, fail]
            "worker_errors": learned.get("worker_errors", {}),
            "budgets": self.config.get("limits", {}).get("daily_budgets", {}),
            "notes": "Delegation statistics. Rebuilt from real task outcomes each run.",
        }
        self.save_model(self.MODEL, model)
        self.log("trained", routes=len(TASK_ROUTES))
        return {"routes": len(TASK_ROUTES), "workers": len(WORKER_ROLES)}

    def routing_stats(self) -> Dict[str, Any]:
        return self.load_model(self.MODEL, default={}) or {}

    def _record_outcome(self, task_type: str, worker: str, ok: bool) -> None:
        model = self.routing_stats()
        success = model.setdefault("success", {})
        entry = success.setdefault(task_type, [0, 0])
        entry[0 if ok else 1] += 1
        errors = model.setdefault("worker_errors", {})
        errors[worker] = int(errors.get(worker, 0)) + (0 if ok else 1)
        self.save_model(self.MODEL, model)

    # ------------------------------------------------------------- task queue
    def submit(self, task_type: str, instruction: str = "", priority: int = 5,
               args: Optional[Dict[str, Any]] = None, worker: Optional[str] = None,
               lead_id: Optional[int] = None) -> int:
        """Queue work.  Returns a task id you can poll with :meth:`task`."""
        with self._lock:
            self._task_seq += 1
            task_id = self._task_seq
            route = TASK_ROUTES.get(task_type)
            if task_type in WORKER_ROLES and instruction:
                # a real sentence goes to the apartment's own handle() - that is
                # what understands 'security scan' / 'quote banao'
                worker, method, kind = task_type, "handle", "text"
            elif worker:
                method, kind = DEFAULT_METHOD.get(worker, ("handle", "text"))
                if route and route[0] == worker:
                    method = route[1]
            elif route:
                worker, method = route[0], route[1]
                kind = DEFAULT_METHOD.get(worker, (method, "text"))[1]
            elif task_type in WORKER_ROLES:
                # empty instruction = batch/timed work (daily cycle)
                worker = task_type
                method, kind = DEFAULT_METHOD[worker]
            else:
                worker, method, kind = "knowledge", "handle", "text"
            self.tasks[task_id] = {
                "id": task_id, "type": task_type, "instruction": instruction,
                "args": args or {}, "priority": int(priority),
                "worker": worker, "method": method, "arg_kind": kind,
                "status": "pending", "attempts": 0, "result": None, "error": "",
                "created_at": time.time(), "started_at": 0.0, "finished_at": 0.0,
                "lead_id": lead_id,
            }
        self._emit("task_queued", task_id=task_id, type=task_type,
                   worker=self.tasks[task_id]["worker"], priority=priority)
        future = self._executor.submit(self._run_task, task_id)
        self.tasks[task_id]["_future"] = future
        return task_id

    def _run_task(self, task_id: int) -> None:
        task = self.tasks[task_id]
        worker_key = task["worker"]
        if worker_key in self._disabled:
            task.update(status="blocked", error="worker disabled",
                        finished_at=time.time())
            self._emit("task_blocked", task_id=task_id, reason="worker disabled")
            return
        while worker_key in self._paused and not self.kill_switch():
            time.sleep(0.25)

        apartment = self.workers.get(worker_key)
        if apartment is None:
            task.update(status="failed", error="worker not loaded: %s" % worker_key,
                        finished_at=time.time())
            self._emit("task_failed", task_id=task_id, reason="worker missing")
            return

        state = self._state(worker_key)
        budget = (self.config.get("limits", {}).get("daily_budgets") or {}).get(worker_key)
        if budget and state.get("calls", 0) >= int(budget):
            task.update(status="blocked", error="daily budget exhausted (%s)" % budget,
                        finished_at=time.time())
            self._emit("task_blocked", task_id=task_id, reason="budget")
            return

        task.update(status="running", started_at=time.time(), attempts=task["attempts"] + 1)
        state["calls"] = int(state.get("calls", 0)) + 1
        state["last_call"] = time.time()
        started = time.time()
        try:
            method_name = task["method"]
            target = getattr(apartment, method_name, None)
            if target is None or not callable(target):
                target, args = apartment.handle, (task["instruction"],
                                                  {"lead_id": task["lead_id"]})
            else:
                args = self._method_args(task.get("arg_kind", "text"), task)
            result = target(*args)
            task.update(status="done", result=_short(result),
                        finished_at=time.time())
            state["consecutive_errors"] = 0
            state["heartbeat"] = time.time()
            self._record_outcome(task["type"], worker_key, True)
            self._emit("task_done", task_id=task_id, worker=worker_key,
                       ms=int((time.time() - started) * 1000))
        except Exception as exc:
            state["errors"] = int(state.get("errors", 0)) + 1
            state["consecutive_errors"] = int(state.get("consecutive_errors", 0)) + 1
            state["last_error"] = str(exc)[:300]
            state["heartbeat"] = time.time()
            task.update(status="failed", error="%s: %s" % (type(exc).__name__, exc),
                        finished_at=time.time())
            self._record_outcome(task["type"], worker_key, False)
            self._emit("task_failed", task_id=task_id, worker=worker_key,
                       error=str(exc)[:200])
            if state["consecutive_errors"] >= int(
                    self.config.get("limits", {}).get("quarantine_after_errors", 4)):
                self._disabled.add(worker_key)
                self._emit("worker_quarantined", worker=worker_key,
                           errors=state["consecutive_errors"])

    @staticmethod
    def _method_args(kind: str, task: Dict[str, Any]) -> tuple:
        """Build the positional arguments for a worker method."""
        args = task.get("args") or {}
        if kind == "city":
            return (args.get("city", "Karachi"), args.get("category", "restaurant"),
                    int(args.get("limit", 20)))
        if kind == "limit":
            return (int(args.get("limit", 10)),)
        if kind == "lead":
            store = default_store()
            lead_id = int(task.get("lead_id") or args.get("lead_id") or 0)
            lead = store.get_lead(lead_id) if lead_id else None
            if lead is None:
                rows = store.leads(limit=1)
                lead = rows[0] if rows else {}
            return (lead,)
        if kind == "none":
            return ()
        return (task.get("instruction", ""), {"lead_id": task.get("lead_id")})

    def task(self, task_id: int) -> Dict[str, Any]:
        task = dict(self.tasks.get(task_id, {}))
        task.pop("_future", None)
        return task

    def task_list(self, status: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            for task in list(self.tasks.values())[-limit:]:
                if status and task.get("status") != status:
                    continue
                clean = {k: v for k, v in task.items() if k != "_future"}
                out.append(clean)
        return out

    def wait(self, task_id: int, timeout: float = 120.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.tasks.get(task_id)
            if task and task.get("status") in ("done", "failed", "blocked"):
                return self.task(task_id)
            time.sleep(0.2)
        return self.task(task_id)

    # ------------------------------------------------------- worker lifecycle
    def pause(self, worker: str) -> str:
        self._paused.add(worker)
        self._emit("worker_paused", worker=worker)
        return "%s paused (queued tasks wait, running tasks finish)." % worker

    def resume(self, worker: str) -> str:
        self._paused.discard(worker)
        self._disabled.discard(worker)
        self._state(worker)["consecutive_errors"] = 0
        self._emit("worker_resumed", worker=worker)
        return "%s resumed." % worker

    def restart(self, worker: str) -> str:
        """Drop and re-import a worker (fresh memory handle, models untouched)."""
        apartment = self.workers.pop(worker, None)
        if apartment is not None:
            try:
                apartment.close()
            except Exception:
                pass
        self._paused.discard(worker)
        self._disabled.discard(worker)
        self.worker_state.pop(worker, None)
        self._emit("worker_restart_requested", worker=worker)
        return ("%s dropped. Orchestrator will re-import it on next use." % worker)

    def disable(self, worker: str) -> str:
        self._disabled.add(worker)
        self._emit("worker_disabled", worker=worker)
        return "%s disabled until you re-enable it." % worker

    def worker_status(self) -> List[Dict[str, Any]]:
        out = []
        for key, apartment in self.workers.items():
            state = self._state(key)
            try:
                trained = bool(apartment.model_names())
            except Exception:
                trained = False
            out.append({
                "worker": key,
                "apartment": "apt_%s" % key,
                "loaded": True,
                "trained": trained,
                "calls": state.get("calls", 0),
                "errors": state.get("errors", 0),
                "consecutive_errors": state.get("consecutive_errors", 0),
                "last_call": state.get("last_call", 0),
                "last_error": state.get("last_error", ""),
                "paused": key in self._paused,
                "disabled": key in self._disabled,
                "offline": bool(getattr(apartment, "offline", False)),
            })
        return out

    # ---------------------------------------------------------------- budgets
    def budget_report(self) -> Dict[str, Any]:
        budgets = self.config.get("limits", {}).get("daily_budgets", {}) or {}
        rows = []
        for key in self.workers:
            state = self._state(key)
            limit = budgets.get(key)
            rows.append({"worker": key, "used": state.get("calls", 0),
                         "limit": limit, "remaining": (int(limit) - state.get("calls", 0))
                         if limit else None})
        return {"budgets": rows, "gate": self.gate.quota_usage()}

    def reset_budgets(self) -> str:
        for key in list(self.worker_state):
            self.worker_state[key]["calls"] = 0
        self._emit("budgets_reset")
        return "Daily call counters reset for all workers."

    # ------------------------------------------------------------ system access
    def terminal(self):
        return self.tool(TERMINAL)

    def run_command(self, command: str, shell: Optional[str] = None,
                    timeout: Optional[float] = None) -> Dict[str, Any]:
        """Execute a real CMD/PowerShell/bash command.  Policy-gated."""
        result = self.terminal().run(command, shell=shell, timeout=timeout)
        self._emit("command", command=command[:160], risk=result.risk,
                   exit_code=result.exit_code, ok=result.ok)
        return result.as_dict()

    def system_info(self) -> Dict[str, Any]:
        return self.terminal().system_info()

    def screenshot(self, name: Optional[str] = None) -> Optional[str]:
        tool = self.tool("screen")
        path = tool.screenshot(name)
        return str(path) if path else None

    def mouse_click(self, x: int, y: int) -> bool:
        return self.tool("mouse").click(int(x), int(y))

    def mouse_move(self, x: int, y: int) -> bool:
        return self.tool("mouse").move(int(x), int(y))

    def type_text(self, text: str) -> bool:
        return self.tool("mouse").type_text(text)

    # ------------------------------------------------------------ emergency
    def emergency_stop(self, reason: str = "manager") -> str:
        """Kill switch: stops every worker mid-flight."""
        from ..base_apartment import BaseApartment
        BaseApartment.set_kill_switch(True)
        self.gate.kill(reason)
        self._emit("emergency_stop", reason=reason)
        return ("EMERGENCY STOP - kill switch set. Sab agents ruke hue hain. "
                "'resume' likh kar wapas chalayein.")

    def resume_all(self) -> str:
        from ..base_apartment import BaseApartment
        BaseApartment.set_kill_switch(False)
        self.gate.revive()
        self._paused.clear()
        self._disabled.clear()
        self._emit("resumed_all")
        return "Kill switch hat gaya. Sab workers wapas kaam par hain."

    # --------------------------------------------------------------- workflows
    def run_pipeline(self, city: str = "Karachi", category: str = "restaurant",
                     limit: int = 10) -> List[int]:
        """Queue hunt -> enrich -> audit -> outreach as a supervised chain."""
        ids = []
        ids.append(self.submit("hunt", "%s mein %s dhoondo" % (city, category),
                               priority=9, args={"city": city, "category": category,
                                                 "limit": limit}))
        ids.append(self.submit("enrich", priority=8, args={"limit": limit}))
        ids.append(self.submit("audit", priority=7, args={"limit": limit}))
        ids.append(self.submit("outreach", priority=6, args={"limit": min(limit, 20)}))
        self._emit("pipeline_queued", task_ids=ids, city=city, category=category)
        return ids

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()

        if any(word in lowered for word in ("stop", "band karo", "emergency", "kill")) and \
                any(word in lowered for word in ("sab", "all", "emergency", "kill switch")):
            return self.emergency_stop("user command")
        if lowered.strip() in ("resume", "restart all", "wapas chalaо", "resume all"):
            return self.resume_all()

        # shell command first - 'git status' is a command, not a status request
        pending_command = self._extract_command(text)
        if pending_command and pending_command.lower() not in (
                "status", "health", "haal", "kaise", "system", "machine", "ram",
                "screenshot", "screen", "task", "tasks", "queue"):
            return str(self.terminal().run(pending_command))

        if any(word in lowered for word in ("status", "health", "kaise", "haal")):
            return self._status_report()
        if any(word in lowered for word in ("budget", "quota", "limit")):
            report = self.budget_report()
            lines = ["Gate quota: %s" % report["gate"]]
            for row in report["budgets"]:
                lines.append("  %-20s used %-4s limit %s" % (row["worker"], row["used"],
                                                             row["limit"] or "-"))
            return "\n".join(lines)
        if any(word in lowered for word in ("system", "ram", "machine", "pc")):
            info = self.system_info()
            return "\n".join("  %-14s %s" % (k, v) for k, v in info.items())
        if any(word in lowered for word in ("screenshot", "screen")):
            path = self.screenshot()
            return "Screenshot: %s" % path if path else "Screenshot nahi ho saka (backend missing)."

        # terminal passthrough (natural-language form)
        command = self._extract_command(text)
        if command is not None:
            return str(self.terminal().run(command))
        if any(word in lowered for word in ("task", "tasks", "queue")):
            return self._task_report()

        # delegation: figure out the task type and dispatch
        task_type = self._infer_task(text)
        route = TASK_ROUTES.get(task_type)
        worker = route[0] if route else "knowledge"
        if worker in self._disabled:
            return "%s disabled hai (baar baar error). 'manager %s resume' chalaayein." % (
                worker, worker)
        task_id = self.submit(task_type, text, priority=7,
                              args=self._infer_args(text, task_type))
        task = self.wait(task_id, timeout=float(self.config.get("limits", {})
                                                .get("task_timeout_s", 180)))
        if task.get("status") == "done":
            return "[%s -> apt_%s] %s" % (task_type, worker, _short(task.get("result"), 2000))
        return "[%s -> apt_%s] %s: %s" % (task_type, worker, task.get("status"),
                                          task.get("error", "")[:300])

    def _extract_command(self, text: str) -> Optional[str]:
        """Detect a shell request in any form and return the raw command.

        Handles 'powershell mein Get-Process chalao', 'cmd mein dir',
        'yeh command chalao: ipconfig' and bare commands like 'git status'.
        """
        lowered = (text or "").strip().lower()
        if not lowered:
            return None
        markers = ("powershell", "pwsh", "cmd", "command prompt", "terminal",
                   "bash", "shell", "command chalao", "chalao", "chala do",
                   "run karo", "execute")
        explicit = any(marker in lowered for marker in markers)
        first = re.split(r"[\s;|&]+", lowered)[0].strip("`\"'")
        try:
            from ...router import COMMAND_VERBS, _allowlisted_exes
            known = first in COMMAND_VERBS or first in _allowlisted_exes()
        except Exception:
            known = False
        if not (explicit or known):
            return None
        command = text
        for noise in ("powershell mein", "powershell me", "pwsh mein", "cmd mein",
                      "cmd me", "terminal mein", "bash mein", "shell mein",
                      "command chalao", "chalao", "chala do", "run karo",
                      "execute karo", "chalayein"):
            command = re.sub(noise, "", command, flags=re.I)
        command = command.strip(" :,-'\"`")
        return command or None

    @staticmethod
    def _infer_task(text: str) -> str:
        lowered = (text or "").lower()
        table = [
            (("dhoondo", "dhondo", "find", "scrape", "leads lao", "leads chahiye"), "hunt"),
            (("email nika", "enrich", "contact details"), "enrich"),
            (("audit", "site check", "score"), "audit"),
            (("message bhejo", "outreach", "pehla message", "contact karo"), "outreach"),
            (("reply", "inbox", "replies"), "inbox"),
            (("brief", "requirements", "sawal"), "requirements"),
            (("mehnga", "objection", "sochna", "trust"), "convince"),
            (("quote", "price", "invoice", "close"), "quote"),
            (("followup", "follow up", "dobara message"), "followup"),
            (("site banao", "demo", "mockup", "build"), "build"),
            (("whatsapp",), "whatsapp"),
            (("linkedin",), "linkedin"),
            (("facebook",), "facebook"),
            (("instagram",), "instagram"),
            (("github", "push"), "github"),
            (("train", "retrain", "seekho"), "train"),
            (("security", "audit log"), "security"),
            (("pricing", "package", "faq", "portfolio"), "knowledge"),
        ]
        for keywords, task_type in table:
            if any(word in lowered for word in keywords):
                return task_type
        return "knowledge"

    @staticmethod
    def _infer_args(text: str, task_type: str) -> Dict[str, Any]:
        import re as _re
        args: Dict[str, Any] = {}
        match = _re.search(r"(\d{1,3})\s*(?:leads?|clients?|business|message)?", text or "")
        if match and task_type in ("hunt", "enrich", "audit", "outreach", "followup"):
            args["limit"] = max(1, min(int(match.group(1)), 40))
        if task_type == "hunt":
            for city in ("Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad",
                         "Multan", "Peshawar", "Quetta", "Sialkot"):
                if city.lower() in (text or "").lower():
                    args["city"] = city
                    break
            for category in ("restaurant", "clinic", "salon", "gym", "pharmacy",
                             "boutique", "school", "hotel", "real_estate",
                             "auto_workshop", "electronics", "law_firm"):
                if category.replace("_", " ") in (text or "").lower() or \
                        category in (text or "").lower():
                    args["category"] = category
                    break
        return args

    def _status_report(self) -> str:
        rows = self.worker_status()
        lines = ["Manager status - %d workers, kill switch: %s"
                 % (len(rows), "ON" if self.kill_switch() else "off")]
        for row in rows:
            flags = []
            if row["paused"]:
                flags.append("paused")
            if row["disabled"]:
                flags.append("DISABLED")
            if not row["trained"]:
                flags.append("untrained")
            if row["offline"]:
                flags.append("offline")
            lines.append("  %-20s calls %-4d errors %-3d %s" % (
                row["apartment"], row["calls"], row["errors"],
                ("[" + ", ".join(flags) + "]") if flags else ""))
        pending = [t for t in self.tasks.values() if t["status"] in ("pending", "running")]
        lines.append("Tasks: %d total, %d in flight, %d done" % (
            len(self.tasks), len(pending),
            len([t for t in self.tasks.values() if t["status"] == "done"])))
        return "\n".join(lines)

    def _task_report(self) -> str:
        tasks = self.task_list(limit=15)
        if not tasks:
            return "Abhi koi task nahi hai."
        return "\n".join("#%s [%s] %s -> apt_%s :: %s" % (
            task["id"], task["status"], task["type"], task["worker"],
            (str(task.get("error") or task.get("result") or ""))[:70])
            for task in tasks)

    def events(self, limit: int = 40) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events[-limit:])

    def run(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Autonomous tick: check worker health, surface problems."""
        problems = []
        for row in self.worker_status():
            if row["disabled"]:
                problems.append({"worker": row["worker"], "issue": "disabled after errors",
                                 "detail": row["last_error"][:120]})
            elif row["consecutive_errors"] >= 2:
                problems.append({"worker": row["worker"], "issue": "repeated errors",
                                 "detail": row["last_error"][:120]})
            elif not row["trained"]:
                problems.append({"worker": row["worker"], "issue": "not trained"})
        if problems:
            self._emit("health_check", problems=len(problems))
        return problems

    def status(self) -> Dict[str, Any]:
        data = super().status()
        data.update({"workers": len(self.workers),
                     "tasks": len(self.tasks),
                     "paused": sorted(self._paused),
                     "disabled": sorted(self._disabled),
                     "kill_switch": self.kill_switch(),
                     "terminal": self.terminal().info()})
        return data


def _short(value: Any, limit: int = 1200) -> Any:
    """Compact a worker's return value for the queue view.

    Strings and dicts survive intact (they are what we print back to the user);
    long lists collapse to a count + preview so the manager never balloons.
    """
    if isinstance(value, (list, tuple)):
        if len(value) <= 10:
            return [_short(v, 300) for v in value]
        return {"count": len(value), "preview": [_short(v, 200) for v in value[:5]]}
    if isinstance(value, dict):
        return {k: _short(v, 300) for k, v in value.items()}
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."
