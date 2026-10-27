"""Live dashboard - real-time view of every agent, every task, and your machine.

Zero dependencies: ``http.server`` + Server-Sent Events, exactly the pattern
recommended for no-dep live dashboards - one ``queue.Queue`` per listener, an
announcer thread that fans messages out, and ``yield f"data: {json}\\n\\n"`` with
``text/event-stream`` [5](https://maxhalford.github.io/blog/flask-sse-no-deps/).
``EventSource`` reconnects on its own, so the page heals after a restart.

Nothing here is mocked. Every number on screen is read live from:

*   the worker instances (calls, errors, pause/disable state, trained models)
*   the manager's task queue (pending / running / done / failed, real results)
*   the SQLite store (live lead counts by status)
*   ``psutil``-free system probing via ``tools/terminal.py`` (RAM, disk, CPU count)
*   the hash-chained audit log (last N real actions)
*   the permission gate (quota usage)

Routes
------
``GET  /``               dashboard HTML
``GET  /api/state``      full JSON snapshot
``GET  /events``         SSE stream (state + log lines)
``POST /api/intent``     send an instruction to the orchestrator  -> reply text
``POST /api/command``    run a terminal command  -> TerminalResult dict
``POST /api/control``    worker control: pause / resume / restart / disable
``POST /api/kill``       emergency stop or resume-all
``POST /api/train``      (re)train one agent or all
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .audit import read_log
from .store import default_store

MAX_BODY = 256 * 1024


# --------------------------------------------------------------------- announcer
class Announcer:
    """Fan-out hub: one queue per connected browser."""

    def __init__(self, history: int = 200):
        self.listeners: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: List[Dict[str, Any]] = []
        self.history_limit = history

    def subscribe(self) -> queue.Queue:
        listener = queue.Queue(maxsize=100)
        with self._lock:
            for event in self._history:
                listener.put(event)
            self.listeners.append(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue) -> None:
        with self._lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def announce(self, kind: str, **payload) -> None:
        event = {"kind": kind, "at": time.time(), **payload}
        with self._lock:
            self._history.append(event)
            if len(self._history) > self.history_limit:
                self._history = self._history[-self.history_limit:]
            dead = []
            for listener in self.listeners:
                try:
                    listener.put_nowait(event)
                except queue.Full:
                    dead.append(listener)
            for listener in dead:
                self.listeners.remove(listener)

    def history_events(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])


# ------------------------------------------------------------------- state read
class StateReader:
    """Pulls a live snapshot out of the running factory."""

    #: last few deliverability checks, so the dashboard shows real work
    recent_verifications: List[Dict[str, Any]] = []

    def __init__(self, orchestrator: Any):
        self.orchestrator = orchestrator

    def snapshot(self) -> Dict[str, Any]:
        orchestrator = self.orchestrator
        manager = getattr(orchestrator, "manager", None)
        agents = getattr(orchestrator, "apartments", {}) or {}
        now = time.time()

        workers: List[Dict[str, Any]] = []
        if manager is not None:
            workers = manager.worker_status()
        for key, apartment in agents.items():
            if key == "manager":
                continue
            if any(row["worker"] == key for row in workers):
                continue
            try:
                models = apartment.model_names()
            except Exception:
                models = []
            workers.append({"worker": key, "apartment": "apt_%s" % key, "loaded": True,
                            "trained": bool(models), "calls": 0, "errors": 0,
                            "consecutive_errors": 0, "last_call": 0, "last_error": "",
                            "paused": False, "disabled": False,
                            "offline": bool(getattr(apartment, "offline", False))})

        tasks = manager.task_list(limit=40) if manager else []
        events = manager.events(limit=25) if manager else []
        counts = self._lead_counts()
        system = self._system(manager)
        audit_tail = self._audit_tail()

        gate_usage: Dict[str, Any] = {}
        try:
            gate_usage = manager.gate.quota_usage() if manager else {}
        except Exception:
            pass

        schedule = self._schedule()
        verify_state = self._verify_state(agents)

        return {
            "now": now,
            "version": getattr(orchestrator, "version", "?"),
            "kill_switch": bool(getattr(manager, "kill_switch", lambda: False)()),
            "workers": workers,
            "tasks": tasks,
            "events": events,
            "leads": counts,
            "system": system,
            "audit": audit_tail,
            "gate": gate_usage,
            "schedule": schedule,
            "verify": verify_state,
            "agent_count": len(agents),
        }

    @staticmethod
    def _lead_counts() -> Dict[str, Any]:
        try:
            store = default_store()
            counts = {k: v for k, v in store.count_leads().items() if k != "total"}
            recent_rows = store.leads(limit=8)
            recent_rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
            return {
                "by_status": counts,
                "total": sum(counts.values()),
                "recent": [{"name": r.get("name") or "-", "city": r.get("city") or "-",
                            "category": r.get("category") or "-",
                            "status": r.get("stage") or "-",
                            "score": r.get("score", 0),
                            "updated": r.get("updated_at", 0)} for r in recent_rows],
            }
        except Exception as exc:
            return {"by_status": {}, "total": 0, "recent": [], "error": str(exc)}

    @staticmethod
    def _schedule() -> Dict[str, Any]:
        """Live send-time windows (Karachi clock, no API, no network)."""
        try:
            from .scheduler import summary
            return summary()
        except Exception as exc:
            return {"error": str(exc), "rows": []}

    @staticmethod
    def _verify_contact(contact: str, agents: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
        """Live deliverability check for one address or number (no API key).

        Routed through the enricher apartment's ``VerifyTool`` when it is up,
        so the MX cache and the counters on the dashboard reflect real work;
        falls back to the plain module functions otherwise.
        """
        agents = agents or {}
        try:
            verify = agents["enricher"].tool("verify")
        except Exception:
            verify = None
        try:
            if verify is not None:
                if "@" in contact:
                    return verify.email(contact)
                return verify.phone(contact)
            from .apartments.tools.verify import email_report, phone_report
            if "@" in contact:
                return email_report(contact)
            return phone_report(contact)
        except Exception as exc:                                  # pragma: no cover
            return {"error": str(exc)}

    @staticmethod
    def _verify_state(agents: Dict[str, Any]) -> Dict[str, Any]:
        """Deliverability tool status - proof that verification is live."""
        try:
            verify = agents["enricher"].tool("verify")
            info = verify.info()
            info["available"] = True
            history = list(StateReader.recent_verifications)
            info["recent"] = history[-8:]
            return info
        except Exception as exc:
            return {"available": False, "error": str(exc)[:120]}

    @staticmethod
    def _system(manager: Any) -> Dict[str, Any]:
        if manager is not None:
            try:
                info = manager.terminal().system_info()
                info["terminal"] = manager.terminal().info()
                return info
            except Exception:
                pass
        return _probe_system()

    @staticmethod
    def _audit_tail(limit: int = 12) -> List[Dict[str, Any]]:
        try:
            entries = read_log(limit=limit)
        except Exception:
            return []
        out = []
        for entry in entries:
            details = entry.get("details")
            if not isinstance(details, dict):
                details = {}
            out.append({"time": entry.get("ts_iso", ""), "actor": entry.get("actor", ""),
                        "action": entry.get("action", ""),
                        "target": str(details)[:60],
                        "result": "%s/%s" % (entry.get("decision", ""),
                                             entry.get("risk", ""))})
        return out


# ------------------------------------------------------------------- http layer
class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ClientHunterDashboard/1.4"
    orchestrator: Any = None
    reader: Optional[StateReader] = None
    announcer: Optional[Announcer] = None

    def log_message(self, fmt: str, *args) -> None:  # keep the console readable
        pass

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, body: str) -> None:
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(min(length, MAX_BODY))
        content_type = self.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                return json.loads(raw.decode("utf-8")) or {}
            except Exception:
                return {}
        parsed = parse_qs(raw.decode("utf-8", "replace"))
        return {key: values[0] for key, values in parsed.items()}

    # --------------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/dashboard"):
            self._html(render_dashboard())
        elif path == "/api/state":
            self._json(self.reader.snapshot())
        elif path == "/api/events":
            self._json(self.announcer.history_events())
        elif path == "/events":
            self._stream()
        elif path == "/api/audit":
            self._json(StateReader._audit_tail(60))
        elif path.startswith("/api/task/"):
            task_id = path.rsplit("/", 1)[-1]
            manager = getattr(self.orchestrator, "manager", None)
            try:
                self._json(manager.task(int(task_id)))
            except Exception as exc:
                self._json({"error": str(exc)}, 404)
        else:
            self._json({"error": "not found: %s" % path}, 404)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        listener = self.announcer.subscribe()
        try:
            while True:
                try:
                    event = listener.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = ("data: %s\n\n" % json.dumps(event, default=str)).encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.announcer.unsubscribe(listener)

    # -------------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self._body()
        manager = getattr(self.orchestrator, "manager", None)
        try:
            if path == "/api/intent":
                text = str(data.get("text", "")).strip()
                if not text:
                    self._json({"error": "empty instruction"}, 400)
                    return
                reply = self.orchestrator.handle(text)
                self.announcer.announce("intent", text=text[:200], reply=_clip(reply))
                self._json({"reply": reply})
            elif path == "/api/command":
                command = str(data.get("command", "")).strip()
                if not command:
                    self._json({"error": "empty command"}, 400)
                    return
                if manager is None:
                    self._json({"error": "manager not loaded"}, 503)
                    return
                result = manager.terminal().run(command, shell=data.get("shell"))
                self.announcer.announce("command", command=command[:200],
                                        risk=result.risk, ok=result.ok)
                self._json(result.as_dict())
            elif path == "/api/control":
                worker = str(data.get("worker", "")).strip()
                action = str(data.get("action", "")).strip()
                if manager is None:
                    self._json({"error": "manager not loaded"}, 503)
                    return
                handler = {"pause": manager.pause, "resume": manager.resume,
                           "disable": manager.disable, "restart": manager.restart}.get(action)
                if handler is None:
                    self._json({"error": "unknown action %s" % action}, 400)
                    return
                self._json({"message": handler(worker), "worker": worker, "action": action})
            elif path == "/api/kill":
                if manager is None:
                    self._json({"error": "manager not loaded"}, 503)
                    return
                message = manager.emergency_stop("dashboard") if data.get("stop") \
                    else manager.resume_all()
                self.announcer.announce("kill", stop=bool(data.get("stop")))
                self._json({"message": message})
            elif path == "/api/train":
                target = str(data.get("agent", "all")).strip()
                results = self.orchestrator.train(target)
                self.announcer.announce("train", target=target)
                self._json({"results": results})
            elif path == "/api/verify":
                contact = str(data.get("contact", "")).strip()
                if not contact:
                    self._json({"error": "empty contact"}, 400)
                else:
                    result = StateReader._verify_contact(
                        contact, getattr(self.orchestrator, "apartments", {}))
                    if "error" not in result:
                        StateReader.recent_verifications.append(result)
                        del StateReader.recent_verifications[:-20]
                        if self.announcer:
                            self.announcer.announce("verify", contact=contact,
                                                    sendable=result.get("sendable"))
                    self._json({"result": result})
            elif path == "/api/submit":
                if manager is None:
                    self._json({"error": "manager not loaded"}, 503)
                    return
                task_id = manager.submit(
                    str(data.get("type", "hunt")), str(data.get("instruction", "")),
                    priority=int(data.get("priority", 7)),
                    args=data.get("args") or {})
                self.announcer.announce("task_queued", task_id=task_id)
                self._json({"task_id": task_id})
            else:
                self._json({"error": "not found: %s" % path}, 404)
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)


class DashboardServer:
    """Owns the HTTP thread and a pusher that emits state on a fixed beat."""

    def __init__(self, orchestrator: Any, host: str = "0.0.0.0", port: int = 8000,
                 push_interval: float = 2.0):
        self.orchestrator = orchestrator
        self.host = host
        self.port = port
        self.push_interval = push_interval
        self.announcer = Announcer()
        self.reader = StateReader(orchestrator)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.started = False
        self._thread: Optional[threading.Thread] = None
        self._pusher: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _make_handler(self):
        namespace = dict(orchestrator=self.orchestrator, reader=self.reader,
                         announcer=self.announcer)
        return type("BoundDashboardHandler", (DashboardHandler,), namespace)

    def start(self, background: bool = True) -> str:
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self.httpd.daemon_threads = True
        self.started = True
        if background:
            self._thread = threading.Thread(target=self.httpd.serve_forever,
                                            name="dashboard-http", daemon=True)
            self._thread.start()
            self._pusher = threading.Thread(target=self._pump, name="dashboard-push",
                                            daemon=True)
            self._pusher.start()
        return self.url

    @property
    def url(self) -> str:
        return "http://%s:%d/" % ("127.0.0.1" if self.host in ("0.0.0.0", "") else self.host,
                                  self.port)

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.reader.snapshot()
                self.announcer.announce("state", state=state)
            except Exception as exc:
                self.announcer.announce("error", error=str(exc))
            self._stop.wait(self.push_interval)

    def serve_forever(self) -> None:
        """Block and serve.  Safe to call after :meth:`start`."""
        if not self.started:
            self.start(background=True)
        try:
            if self.httpd is not None:
                self.httpd.serve_forever()
            else:                                   # pragma: no cover
                while not self._stop.is_set():
                    time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


def _probe_system() -> Dict[str, Any]:
    """Fallback machine probe when the manager (and its terminal) is absent."""
    import os
    import platform
    import shutil
    import sys

    info: Dict[str, Any] = {"platform": sys.platform,
                            "python": sys.version.split()[0],
                            "node": platform.node(), "os": platform.platform()}
    try:
        info["ram_total_mb"] = int(os.sysconf("SC_PAGE_SIZE") *
                                   os.sysconf("SC_PHYS_PAGES") / 1048576)
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(str(__import__("pathlib").Path.home()))
        info["disk_free_gb"] = round(usage.free / 1073741824, 1)
        info["disk_total_gb"] = round(usage.total / 1073741824, 1)
    except Exception:
        pass
    try:
        from .apartments.tools.terminal import find_shells
        info["shells"] = {k: v for k, v in find_shells().items() if v}
    except Exception:
        info["shells"] = {}
    info["terminal"] = {"shells": info["shells"]}
    return info


def _clip(value: Any, limit: int = 800) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


# ------------------------------------------------------------------------- HTML
def render_dashboard() -> str:
    return """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClientHunter Factory - live</title>
<style>
:root{--bg:#0b1020;--panel:#121a30;--line:#1f2b4d;--txt:#e8ecf8;--dim:#8b98bd;
--ok:#31d0aa;--warn:#f7b955;--bad:#ff6b6b;--acc:#5b8cff;--acc2:#a06bff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;background:var(--panel);
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;flex-wrap:wrap}
h1{font-size:16px;margin:0;letter-spacing:.4px}
h1 span{color:var(--acc)}
.pill{padding:3px 10px;border-radius:20px;font-size:12px;border:1px solid var(--line);
background:#0e1630;color:var(--dim)}
.pill.on{color:#0b1020;background:var(--ok);border-color:var(--ok);font-weight:600}
.pill.bad{color:#0b1020;background:var(--bad);border-color:var(--bad);font-weight:600}
.spacer{flex:1}
button{background:var(--acc);color:#04102b;border:0;border-radius:6px;padding:7px 13px;
font-weight:600;cursor:pointer;font-size:13px}
button.ghost{background:transparent;color:var(--txt);border:1px solid var(--line)}
button.danger{background:var(--bad);color:#2a0505}
button:disabled{opacity:.45;cursor:not-allowed}
main{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px}
@media(max-width:1050px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:1px;
color:var(--dim);display:flex;align-items:center;gap:8px}
.card h2 .count{margin-left:auto;color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;
padding:5px 6px;border-bottom:1px solid var(--line)}
td{padding:5px 6px;border-bottom:1px solid #18223f}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.d-ok{background:var(--ok)}.d-warn{background:var(--warn)}.d-bad{background:var(--bad)}
.d-idle{background:#3a456e}
.bar{height:6px;border-radius:4px;background:#1b2440;overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2))}
#console{background:#070c1a;border:1px solid var(--line);border-radius:8px;padding:10px;
height:230px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px;
white-space:pre-wrap;color:#cfe0ff}
#console .cmd{color:var(--warn)}#console .out{color:#a9f5d8}#console .err{color:#ff9d9d}
#console .meta{color:var(--dim)}
input,textarea{width:100%;background:#0a1226;border:1px solid var(--line);color:var(--txt);
border-radius:6px;padding:8px 10px;font:inherit}
textarea{resize:vertical;min-height:64px}
.row{display:flex;gap:8px;margin-top:8px}
.row input{flex:1}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;
background:#1b2440;color:var(--dim);margin-right:4px}
.tag.low{color:#7ee0c0}.tag.medium{color:var(--warn)}.tag.high{color:#ff9d6b}
.tag.critical{color:#fff;background:#b3261e}
.stat{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.stat div{background:#0e1630;border:1px solid var(--line);border-radius:8px;padding:8px 12px;
min-width:96px}
.stat b{display:block;font-size:19px;color:var(--acc)}
.stat small{color:var(--dim);font-size:11px;text-transform:uppercase}
.logline{padding:2px 0;border-bottom:1px solid #17203a;font-size:12px;color:var(--dim)}
.logline b{color:var(--txt);font-weight:600}
.flash{animation:fl 1.2s ease}
@keyframes fl{from{background:#1d2c55}to{background:transparent}}
</style></head><body>
<header>
  <h1>CLIENT<span>HUNTER</span> FACTORY <span id="ver" class="pill">v-</span></h1>
  <span id="conn" class="pill">connecting…</span>
  <span id="kill" class="pill">kill switch: ?</span>
  <div class="spacer"></div>
  <button class="ghost" onclick="refresh()">Refresh</button>
  <button class="ghost" onclick="trainAll()">Retrain all</button>
  <button class="danger" onclick="killSwitch()">EMERGENCY STOP</button>
</header>

<main>
<div>
  <div class="card">
    <h2>Machine <span class="count" id="uptime"></span></h2>
    <div class="stat" id="machine"></div>
  </div>

  <div class="card">
    <h2>Leads <span class="count" id="leadtotal">0</span></h2>
    <div class="stat" id="leadstats"></div>
    <table><thead><tr><th>Business</th><th>City</th><th>Niche</th><th>Status</th></tr></thead>
      <tbody id="leads"></tbody></table>
  </div>

  <div class="card">
    <h2>Intent console</h2>
    <textarea id="intent" placeholder="Karachi mein restaurants dhoondo aur 10 leads lao"></textarea>
    <div class="row"><button onclick="sendIntent()">Send to manager</button></div>
    <div id="intentreply" class="mono" style="margin-top:8px;color:#a9f5d8;white-space:pre-wrap"></div>
  </div>

  <div class="card">
    <h2>Terminal <span class="count" id="shells"></span></h2>
    <div class="row"><input id="cmd" placeholder="Get-Process | Select-Object -First 5"
      onkeydown="if(event.key==='Enter')runCmd()"><button onclick="runCmd()">Run</button></div>
    <div class="row">
      <select id="shell" style="background:#0a1226;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:8px">
        <option value="">auto</option><option value="powershell">PowerShell</option>
        <option value="cmd">CMD</option><option value="bash">bash</option></select>
      <button class="ghost" onclick="quick('systeminfo')">systeminfo</button>
      <button class="ghost" onclick="quick('ipconfig')">ipconfig</button>
      <button class="ghost" onclick="quick('Get-Process | Sort-Object CPU -Descending | Select-Object -First 8')">top CPU</button>
    </div>
    <div id="console"></div>
  </div>
</div>

<div>
  <div class="card">
    <h2>Agents <span class="count" id="agentcount">0</span></h2>
    <table><thead><tr><th>Apartment</th><th>State</th><th>Calls</th><th>Err</th>
      <th>Last error</th><th>Control</th></tr></thead><tbody id="workers"></tbody></table>
  </div>

  <div class="card">
    <h2>Tasks</h2>
    <table><thead><tr><th>#</th><th>Type</th><th>Worker</th><th>Status</th><th>Result</th></tr></thead>
      <tbody id="tasks"></tbody></table>
  </div>

  <div class="card">
    <h2>Send windows <span class="count" id="schedclock">–</span></h2>
    <div id="jummah" style="margin-bottom:8px"></div>
    <table><thead><tr><th>Channel</th><th>Now</th><th>Next slot</th><th>Windows</th></tr></thead>
      <tbody id="schedule"></tbody></table>
  </div>

  <div class="card">
    <h2>Deliverability <span class="count" id="verifybackend">–</span></h2>
    <div id="verifystats" class="mono" style="margin-bottom:8px"></div>
    <table><thead><tr><th>Address</th><th>Score</th><th>Verdict</th><th>Why</th></tr></thead>
      <tbody id="verifyrows"></tbody></table>
    <input id="verifyinput" placeholder="email ya phone (e.g. info@bundukhan.pk / 03001234567)"
           style="margin-top:8px">
    <div style="margin-top:8px;display:flex;gap:8px">
      <button class="ghost" onclick="checkContact()">Check</button>
      <span id="verifyreply" class="mono" style="color:#a9f5d8"></span>
    </div>
  </div>

  <div class="card">
    <h2>Audit log (hash-chained)</h2>
    <div id="audit" style="max-height:190px;overflow:auto"></div>
  </div>
</div>
</main>

<script>
let es=null, lastState=null;
function fmtTime(t){const d=new Date(t*1000);return d.toLocaleTimeString()}
function el(id){return document.getElementById(id)}

function connect(){
  es=new EventSource('/events');
  es.onopen=()=>{el('conn').textContent='live';el('conn').className='pill on'};
  es.onerror=()=>{el('conn').textContent='reconnecting…';el('conn').className='pill bad'};
  es.onmessage=(e)=>{
    const msg=JSON.parse(e.data);
    if(msg.kind==='state'){paint(msg.state);lastState=msg.state}
    else if(msg.kind==='command'){logLine('cmd','$ '+msg.command);
      logLine('meta','risk='+msg.risk+' ok='+msg.ok)}
    else if(msg.kind==='intent'){logLine('meta','intent: '+msg.text)}
    else if(msg.kind==='kill'){flash();}
    else if(msg.kind==='train'){logLine('meta','training: '+msg.target)}
  };
}

function flash(){document.body.classList.add('flash');setTimeout(()=>document.body.classList.remove('flash'),1200)}

function paint(s){
  el('ver').textContent='v'+(s.version||'?');
  el('kill').textContent='kill switch: '+(s.kill_switch?'ON':'off');
  el('kill').className='pill '+(s.kill_switch?'bad':'on');
  el('agentcount').textContent=s.workers.length;
  el('uptime').textContent=new Date(s.now*1000).toLocaleTimeString();

  let m='';
  for(const [k,v] of Object.entries(s.system||{})){
    if(k==='terminal')continue;
    m+='<div><b>'+esc(String(v))+'</b><small>'+esc(k)+'</small></div>';
  }
  el('machine').innerHTML=m;
  const t=(s.system&&s.system.terminal)||{};
  el('shells').textContent=(t.shells?Object.entries(t.shells).filter(([,v])=>v).map(([k])=>k).join(', '):'')||'none detected';

  const L=s.leads||{};el('leadtotal').textContent=L.total||0;
  let ls='';for(const [k,v] of Object.entries(L.by_status||{}))ls+='<div><b>'+v+'</b><small>'+esc(k)+'</small></div>';
  el('leadstats').innerHTML=ls||'<small>no leads yet</small>';
  el('leads').innerHTML=(L.recent||[]).map(r=>'<tr><td>'+esc(r.name)+'</td><td>'+esc(r.city)+
    '</td><td>'+esc(r.category)+'</td><td>'+esc(r.status)+'</td></tr>').join('');

  el('workers').innerHTML=s.workers.map(w=>{
    const cls=w.disabled?'d-bad':(w.paused?'d-warn':(w.errors>0?'d-warn':'d-ok'));
    const state=w.disabled?'disabled':(w.paused?'paused':(w.trained?'trained':'untrained'));
    return '<tr><td class="mono">'+esc(w.apartment)+'</td>'+
      '<td><span class="dot '+cls+'"></span>'+esc(state)+(w.offline?' offline':'')+'</td>'+
      '<td>'+w.calls+'</td><td>'+w.errors+'</td>'+
      '<td class="mono" style="color:#ff9d9d">'+esc((w.last_error||'').slice(0,52))+'</td>'+
      '<td><button class="ghost" style="padding:3px 7px" onclick="ctrl(\''+w.worker+'\',\'pause\')">pause</button> '+
      '<button class="ghost" style="padding:3px 7px" onclick="ctrl(\''+w.worker+'\',\'resume\')">resume</button> '+
      '<button class="ghost" style="padding:3px 7px" onclick="ctrl(\''+w.worker+'\',\'restart\')">restart</button></td></tr>';
  }).join('');

  el('tasks').innerHTML=(s.tasks||[]).slice().reverse().slice(0,18).map(t=>{
    const out=t.error?('<span style="color:#ff9d9d">'+esc(String(t.error).slice(0,60))+'</span>')
      :esc(JSON.stringify(t.result||'').slice(0,60));
    return '<tr><td>'+t.id+'</td><td>'+esc(t.type)+'</td><td class="mono">apt_'+esc(t.worker)+
      '</td><td>'+esc(t.status)+'</td><td class="mono">'+out+'</td></tr>';
  }).join('')||'<tr><td colspan="5">no tasks yet</td></tr>';

  const S=s.schedule||{};
  el('schedclock').textContent=S.now||'–';
  el('jummah').innerHTML=(S.jummah?'<span class="pill bad">Jummah break</span>':
    (S.ramadan?'<span class="pill">Ramadan mode</span>':''));
  el('schedule').innerHTML=(S.rows||[]).map(r=>{
    const cls=r.send_now?'d-ok':'d-idle';
    return '<tr><td class="mono">'+esc(r.platform)+'</td>'+
      '<td><span class="dot '+cls+'"></span>'+(r.send_now?'send':'wait')+'</td>'+
      '<td class="mono">'+esc(r.next_slot)+'</td>'+
      '<td class="mono" style="color:#8b98bd">'+esc((r.windows||[]).join(', '))+'</td></tr>';
  }).join('')||'<tr><td colspan="4">'+(S.error?esc(S.error):'n/a')+'</td></tr>';

  const V=s.verify||{};
  el('verifybackend').textContent=V.available?('dns '+(V.resolvers||[]).length+' · '+(V.phone_backend||'')):'off';
  const vs=V.stats||{};
  el('verifystats').textContent=V.available
    ? ('checked '+vs.emails+' emails · '+vs.phones+' phones · '+vs.mx_lookups+' MX lookups · '+vs.sendable+' sendable')
    : ('unavailable: '+esc(V.error||''));
  el('verifyrows').innerHTML=(V.recent||[]).map(r=>{
    const okCls=r.sendable?'d-ok':'d-bad';
    return '<tr><td class="mono">'+esc(r.email||r.e164||'')+'</td><td>'+
      Number(r.score||0).toFixed(2)+'</td>'+
      '<td><span class="dot '+okCls+'"></span>'+(r.sendable?'sendable':'skip')+'</td>'+
      '<td class="mono" style="color:#8b98bd">'+esc((r.reasons||[]).join(', ').slice(0,48))+'</td></tr>';
  }).join('')||'<tr><td colspan="4">koi check abhi nahi hua</td></tr>';

  el('audit').innerHTML=(s.audit||[]).map(a=>'<div class="logline"><b>'+esc(a.actor)+
    '</b> '+esc(a.action)+' <span class="mono">'+esc(a.target)+'</span> → '+esc(a.result)+'</div>').join('');
}

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function logLine(cls,txt){const c=el('console');const d=document.createElement('div');
  d.className=cls;d.textContent=txt;c.appendChild(d);c.scrollTop=c.scrollHeight;
  while(c.childNodes.length>400)c.removeChild(c.firstChild)}

async function post(url,body){const r=await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  return await r.json()}

function refresh(){fetch('/api/state').then(r=>r.json()).then(paint)}
async function sendIntent(){
  const t=el('intent').value.trim();if(!t)return;
  el('intentreply').textContent='working…';
  const r=await post('/api/intent',{text:t});
  el('intentreply').textContent=r.reply||r.error;
}
async function checkContact(){
  const v=el('verifyinput').value.trim();if(!v)return;
  el('verifyreply').textContent='checking…';
  const r=await post('/api/verify',{contact:v});
  el('verifyreply').textContent=r.error?r.error:JSON.stringify(r.result);
  el('verifyinput').value='';
  refresh();
}

async function runCmd(){
  const c=el('cmd').value.trim();if(!c)return;
  el('cmd').value='';logLine('cmd','$ '+c);
  const r=await post('/api/command',{command:c,shell:el('shell').value});
  if(r.error){logLine('err',r.error);return}
  logLine('meta',r.shell+' · risk='+r.risk+' · exit='+r.exit_code+' · '+r.duration_ms+'ms');
  if(r.stdout)logLine('out',r.stdout.slice(0,4000));
  if(r.stderr)logLine('err',r.stderr.slice(0,2000));
  if(r.blocked_reason)logLine('err','BLOCKED: '+r.blocked_reason);
}
function quick(c){el('cmd').value=c;runCmd()}
async function ctrl(worker,action){const r=await post('/api/control',{worker,action});
  logLine('meta',(r.message||r.error));refresh()}
async function killSwitch(){const r=await post('/api/kill',{stop:true});
  logLine('err',r.message||r.error);refresh()}
async function trainAll(){logLine('meta','training started…');
  const r=await post('/api/train',{agent:'all'});
  logLine('meta','trained: '+Object.keys(r.results||{}).length+' apartments');refresh()}

connect();
</script></body></html>
"""
