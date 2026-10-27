"""BaseApartment - the contract every agent in the factory lives by.

An "apartment" is an isolated home for one agent.  Isolation is enforced here,
not by convention:

*   **Files** - the apartment can only touch paths under its own ``sandbox/``
    folder.  ``../``, absolute paths and symlinks are rejected.
*   **Network / email / WhatsApp / browser** - every dangerous capability is a
    named permission.  It is granted only when *both* the global matrix
    (``config/permissions.json``) and the apartment's own ``config.json`` say
    yes.  The orchestrator can additionally flip a global kill switch.
*   **Memory** - each apartment owns ``memory.db`` (SQLite).  There is no API
    on this class to read another apartment's database.
*   **Models** - saved and loaded as JSON only.  Pickle is refused outright
    (unpickling == arbitrary code execution).

Everything is stdlib: ``json``, ``sqlite3``, ``pathlib``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .. import FACTORY_ROOT
from ..permissions.gate import default_gate

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PermissionDenied(PermissionError):
    """Raised when an apartment tries to use a capability it does not have."""


class ModelMissing(FileNotFoundError):
    """Raised when an apartment is asked to use a model that was never trained."""


class TrainingError(RuntimeError):
    """Raised when an apartment's own training data is missing or unusable."""


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #

#: Every capability the factory knows about, with a safe default.
PERMISSION_CATALOG = {
    "internet": False,     # outbound HTTP(S) fetch / scrape
    "email": False,        # SMTP send + IMAP read
    "whatsapp": False,     # WhatsApp Web automation
    "browser": False,      # launch/attach Chrome
    "file_read": True,     # read inside own sandbox + shared data/
    "file_write": True,    # write inside own sandbox
    "subprocess": False,   # spawn processes (browser only)
    "shared_write": True,  # write to the shared leads DB
}

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _check_component(name: str) -> str:
    """Guard against path traversal in file/model names."""
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise ValueError("unsafe name (allowed: letters, digits, _ . -, max 64): %r" % (name,))
    if name in (".", "..") or ".." in name:
        raise ValueError("path traversal refused: %r" % (name,))
    return name


class ApartmentDB:
    """Private key/value + event log store for one apartment (SQLite)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=15.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,
                payload TEXT DEFAULT '{}',
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS items (
                key   TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            );
            """
        )
        self._conn.commit()

    # kv -------------------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def all(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM kv").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except Exception:
                out[r["key"]] = r["value"]
        return out

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            self._conn.commit()

    # items (larger blobs: briefs, deals, transcripts) ----------------------
    def put_item(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO items (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            self._conn.commit()

    def get_item(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def item_keys(self, prefix: str = "") -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM items WHERE key LIKE ? ORDER BY key",
                (prefix + "%",)).fetchall()
        return [r["key"] for r in rows]

    # events ----------------------------------------------------------------
    def log(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (kind, payload, created_at) VALUES (?,?,?)",
                (kind, json.dumps(payload or {}, ensure_ascii=False)[:4000], time.time()),
            )
            self._conn.commit()

    def events(self, limit: int = 50, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?",
                    (kind, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
            out.append(d)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class BaseApartment:
    """Abstract base class for every agent in the factory.

    Subclasses set :attr:`NAME`, implement :meth:`train` and :meth:`handle`.
    """

    #: Apartment key, must match its folder name (``apt_<NAME>``) minus prefix.
    NAME = "base"
    #: Human readable one-liner used by the orchestrator's ``help`` output.
    DESCRIPTION = "Abstract apartment"
    #: Permissions this apartment *requests*.  The effective permission is the
    #: logical AND of the global matrix, this dict, and any runtime override.
    PERMISSIONS: Dict[str, bool] = {}

    _GLOBAL_PERMISSIONS: Optional[Dict[str, Any]] = None
    _PERMISSION_LOCK = threading.RLock()
    _KILL_SWITCH = False

    # ------------------------------------------------------------------ setup
    def __init__(self, factory_root: Optional[os.PathLike] = None,
                 overrides: Optional[Dict[str, bool]] = None,
                 offline: bool = False):
        root = Path(factory_root) if factory_root else FACTORY_ROOT
        self.root = root
        self.home = root / "apartments" / ("apt_%s" % self.NAME)
        self.models_dir = self.home / "models"
        self.sandbox = self.home / "sandbox"
        for folder in (self.home, self.models_dir, self.sandbox):
            folder.mkdir(parents=True, exist_ok=True)

        self.offline = bool(offline)
        self.config = self._load_config()
        self.train_data = self._load_train_data()
        self.db = ApartmentDB(self.home / "memory.db")
        #: Central permission gate (shared process-wide so quotas are global).
        self.gate = default_gate()
        #: Runtime permission overrides (orchestrator kill switch, --offline, ...)
        self.overrides: Dict[str, bool] = dict(overrides or {})
        if self.offline:
            self.overrides.setdefault("internet", False)
            self.overrides.setdefault("email", False)
            self.overrides.setdefault("whatsapp", False)
            self.overrides.setdefault("browser", False)
        self._tools: Dict[str, Any] = {}
        self._started_at = time.time()

    # ------------------------------------------------------------- config i/o
    def _load_config(self) -> Dict[str, Any]:
        path = self.home / "config.json"
        if not path.exists():
            return {"name": self.NAME, "permissions": dict(self.PERMISSIONS), "limits": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - corrupted config
            raise TrainingError("%s: cannot parse config.json: %s" % (self.NAME, exc))

    def _load_train_data(self) -> Any:
        path = self.home / "train.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover
            raise TrainingError("%s: cannot parse train.json: %s" % (self.NAME, exc))

    @classmethod
    def global_permissions(cls) -> Dict[str, Any]:
        """Load (and cache) ``config/permissions.json``."""
        with cls._PERMISSION_LOCK:
            if cls._GLOBAL_PERMISSIONS is None:
                path = FACTORY_ROOT / "config" / "permissions.json"
                data: Dict[str, Any] = {}
                if path.exists():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:
                        data = {}
                cls._GLOBAL_PERMISSIONS = data
            return cls._GLOBAL_PERMISSIONS

    @classmethod
    def reset_permission_cache(cls) -> None:
        with cls._PERMISSION_LOCK:
            cls._GLOBAL_PERMISSIONS = None

    @classmethod
    def set_kill_switch(cls, value: bool) -> None:
        """Global emergency stop - kills network/email/whatsapp/browser for all."""
        with cls._PERMISSION_LOCK:
            cls._KILL_SWITCH = bool(value)

    @classmethod
    def kill_switch(cls) -> bool:
        return cls._KILL_SWITCH

    # ------------------------------------------------------------ permissions
    def permission_sources(self) -> Dict[str, Dict[str, bool]]:
        """Return the three permission layers, for debugging / auditing."""
        g = self.global_permissions()
        glob = dict(PERMISSION_CATALOG)
        glob.update(g.get("global", {}) or {})
        mine = dict(self.PERMISSIONS)
        mine.update(self.config.get("permissions", {}) or {})
        apt_layer = (g.get("apartments", {}) or {}).get("apt_%s" % self.NAME, {}) or {}
        return {"global": glob, "config_json": mine, "matrix": apt_layer,
                "override": dict(self.overrides)}

    def can(self, permission: str) -> bool:
        """Effective permission = global AND config.json AND matrix AND override."""
        if self._KILL_SWITCH and permission in ("internet", "email", "whatsapp", "browser",
                                                "subprocess"):
            return False
        src = self.permission_sources()
        value = True
        for layer in ("global", "config_json", "matrix", "override"):
            if permission in src[layer]:
                value = value and bool(src[layer][permission])
        return value

    def require(self, permission: str) -> None:
        """Raise :class:`PermissionDenied` unless the capability is granted."""
        if not self.can(permission):
            raise PermissionDenied(
                "apt_%s is not allowed to use %r (effective=%s)"
                % (self.NAME, permission, self.can(permission))
            )

    def permission_report(self) -> Dict[str, bool]:
        return {p: self.can(p) for p in PERMISSION_CATALOG}

    # ------------------------------------------------------------ action gate
    def permitted(self, action: str, details: Optional[Dict[str, Any]] = None,
                  interactive: Optional[bool] = None):
        """Ask the gate whether this action is allowed (never raises)."""
        return self.gate.check(self.NAME, action, details or {}, interactive)

    def permit(self, action: str, details: Optional[Dict[str, Any]] = None,
               interactive: Optional[bool] = None):
        """Assert an action is allowed - raises :class:`Denied` otherwise.

        Every tool in ``apartments/tools/`` calls this before doing anything
        with an external effect, so there is exactly one enforcement point.
        """
        from ..permissions.gate import Denied
        decision = self.gate.check(self.NAME, action, details or {}, interactive)
        if not decision.allowed:
            raise Denied("[apt_%s] %s denied: %s" % (self.NAME, action, decision.reason))
        return decision

    # ----------------------------------------------------------------- models
    def model_path(self, name: str) -> Path:
        _check_component(name)
        if not name.endswith(".json"):
            name += ".json"
        return self.models_dir / name

    def has_model(self, name: str) -> bool:
        return self.model_path(name).exists()

    def save_model(self, name: str, obj: Any) -> Path:
        """Atomically write a JSON model.  Refuses anything non-JSON-serialisable."""
        path = self.model_path(name)
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str)
        # Round-trip to prove it really is JSON-safe (no python objects smuggled in).
        json.loads(payload)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load_model(self, name: str, default: Any = None) -> Any:
        """Load a JSON model.  Returns ``default`` when never trained."""
        path = self.model_path(name)
        if not path.exists():
            if default is None:
                raise ModelMissing(
                    "apt_%s has no model %r - run: python -m factory.train_all" % (self.NAME, name))
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover
            raise TrainingError("%s: corrupt model %s: %s" % (self.NAME, name, exc))

    def model_names(self) -> List[str]:
        return sorted(p.name for p in self.models_dir.glob("*.json"))

    # ----------------------------------------------------------------- memory
    def remember(self, key: str, value: Any) -> None:
        self.db.set(key, value)

    def recall(self, key: str, default: Any = None) -> Any:
        return self.db.get(key, default)

    def log(self, kind: str, **payload) -> None:
        self.db.log(kind, payload)

    # -------------------------------------------------------------- sandboxed io
    def sandbox_path(self, relpath: str) -> Path:
        """Resolve ``relpath`` inside this apartment's sandbox, refusing escapes."""
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise PermissionDenied("apt_%s: path escapes sandbox: %r" % (self.NAME, relpath))
        target = (self.sandbox / rel).resolve()
        root = self.sandbox.resolve()
        if not str(target).startswith(str(root) + os.sep) and target != root:
            raise PermissionDenied("apt_%s: path escapes sandbox: %r" % (self.NAME, relpath))
        return target

    def write_text(self, relpath: str, text: str, append: bool = False) -> Path:
        self.require("file_write")
        target = self.sandbox_path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as fh:
            fh.write(text)
        return target

    def read_text(self, relpath: str, default: Optional[str] = None) -> Optional[str]:
        self.require("file_read")
        target = self.sandbox_path(relpath)
        if not target.exists():
            if default is None:
                raise FileNotFoundError(target)
            return default
        return target.read_text(encoding="utf-8", errors="replace")

    def write_json(self, relpath: str, obj: Any) -> Path:
        return self.write_text(relpath, json.dumps(obj, ensure_ascii=False, indent=1))

    def read_json(self, relpath: str, default: Any = None) -> Any:
        raw = self.read_text(relpath, default=None)
        if raw is None:
            return default
        return json.loads(raw)

    def list_sandbox(self, sub: str = "") -> List[str]:
        base = self.sandbox_path(sub) if sub else self.sandbox
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.sandbox)) for p in base.rglob("*") if p.is_file())

    # ------------------------------------------------------------------ shared
    def shared_data_path(self, name: str) -> Path:
        """Read-only access to ``factory/data/`` (the user's training corpus)."""
        _check_component(name)
        path = (self.root / "data" / name).resolve()
        root = (self.root / "data").resolve()
        if not str(path).startswith(str(root) + os.sep):
            raise PermissionDenied("apt_%s: shared data escape: %r" % (self.NAME, name))
        return path

    def load_shared_data(self, name: str, default: Any = None) -> Any:
        """Load a JSON file from ``factory/data/`` (read-only, no permission needed)."""
        path = self.shared_data_path(name)
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TrainingError("%s: cannot parse data/%s: %s" % (self.NAME, name, exc))

    def agency(self) -> Dict[str, Any]:
        """Agency profile: name, services, packages, pricing, contact.

        Lives in ``factory/config/agency.json`` (fallback: ``factory/data/``),
        because it is configuration, not training corpus.
        """
        for folder, name in (("config", "agency.json"), ("data", "agency.json")):
            path = (self.root / folder / name).resolve()
            root = (self.root / folder).resolve()
            if str(path).startswith(str(root) + os.sep) and path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8")) or {}
                except Exception as exc:
                    raise TrainingError("%s: cannot parse %s/%s: %s"
                                        % (self.NAME, folder, name, exc))
        return {}

    # ------------------------------------------------------------------- tools
    def tool(self, name: str):
        """Lazily construct and cache a tool bound to this apartment."""
        if name in self._tools:
            return self._tools[name]
        mod = __import__("factory.apartments.tools.%s" % name, fromlist=["*"])
        factory_method = getattr(mod, "build", None)
        if factory_method is None:
            raise AttributeError("tool %r has no build(apartment) factory" % name)
        tool = factory_method(self)
        self._tools[name] = tool
        return tool

    # --------------------------------------------------------------- lifecycle
    def train(self) -> Dict[str, Any]:
        """Train this apartment from its own data.  Must return a stats dict."""
        raise NotImplementedError

    def is_trained(self) -> bool:
        return bool(self.model_names())

    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Answer one user/lead message.  Must be implemented by subclasses."""
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        """Compact health report used by ``orchestrator --status``."""
        return {
            "apartment": "apt_%s" % self.NAME,
            "description": self.DESCRIPTION,
            "trained": self.is_trained(),
            "models": self.model_names(),
            "permissions": {k: v for k, v in self.permission_report().items() if v},
            "memory_events": len(self.db.events(limit=1000)),
            "uptime_s": round(time.time() - self._started_at, 1),
        }

    def describe(self) -> str:
        return "%s: %s" % ("apt_%s" % self.NAME, self.DESCRIPTION)

    def close(self) -> None:
        self.db.close()

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<%s apt_%s>" % (type(self).__name__, self.NAME)
