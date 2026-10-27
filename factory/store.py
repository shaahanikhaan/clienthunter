"""Shared SQLite store: leads, messages, events, audit cache.

Isolation rule: an apartment can never open this file directly through its own
sandboxed file tool.  It reaches shared data only through this module, and only
when the permission matrix says it may.  Private per-apartment memories stay in
``apartments/<name>/memory.db``.

Everything is plain ``sqlite3`` with WAL off (single writer) and small
transactions so the whole thing fits comfortably in a 512 MB budget on a
4 GB machine.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import FACTORY_ROOT

DB_PATH = FACTORY_ROOT / "data" / "factory.db"

_LOCAL = threading.local()

LEAD_COLUMNS = (
    "id",
    "name",
    "owner_name",
    "phone",
    "email",
    "address",
    "category",
    "city",
    "website",
    "rating",
    "reviews",
    "socials",
    "site_status",
    "audit_score",
    "audit_issues",
    "stage",
    "score",
    "source",
    "notes",
    "created_at",
    "updated_at",
    "last_contact_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    owner_name      TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    address         TEXT DEFAULT '',
    category        TEXT DEFAULT '',
    city            TEXT DEFAULT '',
    website         TEXT DEFAULT '',
    rating          REAL DEFAULT 0,
    reviews         INTEGER DEFAULT 0,
    socials         TEXT DEFAULT '{}',
    site_status     TEXT DEFAULT 'unknown',
    audit_score     INTEGER DEFAULT -1,
    audit_issues    TEXT DEFAULT '[]',
    stage           TEXT DEFAULT 'new',
    score           INTEGER DEFAULT 0,
    source          TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    created_at      REAL DEFAULT 0,
    updated_at      REAL DEFAULT 0,
    last_contact_at REAL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_dedupe
    ON leads(name, city, category);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER,
    direction   TEXT,
    channel     TEXT,
    body        TEXT,
    intent      TEXT DEFAULT '',
    sentiment   TEXT DEFAULT '',
    priority    TEXT DEFAULT '',
    created_at  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT,
    kind       TEXT,
    payload    TEXT DEFAULT '{}',
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor);

CREATE TABLE IF NOT EXISTS followups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    due_at      REAL NOT NULL,
    touch       INTEGER DEFAULT 1,
    angle       TEXT DEFAULT '',
    body        TEXT DEFAULT '',
    channel     TEXT DEFAULT 'whatsapp',
    status      TEXT DEFAULT 'pending',
    created_at  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, due_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> float:
    return time.time()


class Store:
    """Thin, thread-safe wrapper around the shared factory database."""

    def __init__(self, path: Optional[os.PathLike] = None):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False: the manager runs apartments in worker threads;
        # every access is serialised through self._lock, so this is safe.
        self._conn = sqlite3.connect(str(self.path), timeout=15.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA cache_size=-8000")  # ~8 MB page cache
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- plumbing
    def execute(self, sql: str, params: Iterable = ()):
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------- leads
    def upsert_lead(self, lead: Dict[str, Any]) -> int:
        """Insert a lead, or merge into an existing one keyed on name+city+category."""
        clean = {k: lead.get(k, "" if k not in ("rating", "reviews", "audit_score", "score") else 0)
                 for k in LEAD_COLUMNS if k != "id"}
        now = _now()
        clean.setdefault("created_at", now)
        clean["updated_at"] = now
        if not clean.get("created_at"):
            clean["created_at"] = now
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at FROM leads WHERE name=? AND city=? AND category=?",
                (clean["name"], clean["city"], clean["category"]),
            ).fetchone()
            if row:
                lead_id = int(row["id"])
                clean["created_at"] = row["created_at"]
                sets = ", ".join("%s=?" % c for c in clean)
                self._conn.execute(
                    "UPDATE leads SET %s WHERE id=?" % sets,
                    tuple(clean.values()) + (lead_id,),
                )
            else:
                cols = ", ".join(clean)
                marks = ", ".join("?" * len(clean))
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO leads (%s) VALUES (%s)" % (cols, marks),
                    tuple(clean.values()),
                )
                lead_id = int(cur.lastrowid or 0)
                if not lead_id:
                    row = self._conn.execute(
                        "SELECT id FROM leads WHERE name=? AND city=? AND category=?",
                        (clean["name"], clean["city"], clean["category"]),
                    ).fetchone()
                    lead_id = int(row["id"]) if row else 0
            self._conn.commit()
        return lead_id

    def get_lead(self, lead_id: int) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT * FROM leads WHERE id=?", (lead_id,))
        return _row_to_lead(rows[0]) if rows else None

    def leads(self, stage: Optional[str] = None, city: Optional[str] = None,
              limit: int = 500, min_score: int = -1) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM leads WHERE score >= ?"
        params: List[Any] = [min_score]
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        if city:
            sql += " AND city=?"
            params.append(city)
        sql += " ORDER BY score DESC, id ASC LIMIT ?"
        params.append(limit)
        return [_row_to_lead(r) for r in self.query(sql, params)]

    def search_leads(self, needle: str, limit: int = 20) -> List[Dict[str, Any]]:
        like = "%%%s%%" % (needle or "")
        rows = self.query(
            "SELECT * FROM leads WHERE name LIKE ? OR city LIKE ? OR category LIKE ?"
            " OR phone LIKE ? OR email LIKE ? LIMIT ?",
            (like, like, like, like, like, limit),
        )
        return [_row_to_lead(r) for r in rows]

    def set_stage(self, lead_id: int, stage: str, score: Optional[int] = None) -> None:
        if score is None:
            self.execute("UPDATE leads SET stage=?, updated_at=? WHERE id=?", (stage, _now(), lead_id))
        else:
            self.execute(
                "UPDATE leads SET stage=?, score=?, updated_at=? WHERE id=?",
                (stage, int(score), _now(), lead_id),
            )

    def touch(self, lead_id: int) -> None:
        self.execute("UPDATE leads SET last_contact_at=?, updated_at=? WHERE id=?",
                     (_now(), _now(), lead_id))

    def update_fields(self, lead_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        sets = ", ".join("%s=?" % k for k in fields)
        self.execute("UPDATE leads SET %s WHERE id=?" % sets, tuple(fields.values()) + (lead_id,))

    def count_leads(self) -> Dict[str, int]:
        rows = self.query("SELECT stage, COUNT(*) AS n FROM leads GROUP BY stage")
        out = {r["stage"]: r["n"] for r in rows}
        out["total"] = sum(out.values())
        return out

    # ---------------------------------------------------------------- messages
    def log_message(self, lead_id: Optional[int], direction: str, channel: str, body: str,
                    intent: str = "", sentiment: str = "", priority: str = "") -> int:
        cur = self.execute(
            "INSERT INTO messages (lead_id, direction, channel, body, intent, sentiment,"
            " priority, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (lead_id, direction, channel, body[:4000], intent, sentiment, priority, _now()),
        )
        return int(cur.lastrowid or 0)

    def messages(self, lead_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM messages WHERE lead_id=? ORDER BY id DESC LIMIT ?", (lead_id, limit))
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- followups
    def schedule_followup(self, lead_id: int, due_at: float, touch: int, angle: str,
                          body: str, channel: str = "whatsapp") -> int:
        cur = self.execute(
            "INSERT INTO followups (lead_id, due_at, touch, angle, body, channel, status,"
            " created_at) VALUES (?,?,?,?,?,?, 'pending', ?)",
            (lead_id, due_at, touch, angle, body[:4000], channel, _now()),
        )
        return int(cur.lastrowid or 0)

    def due_followups(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = now or _now()
        rows = self.query(
            "SELECT * FROM followups WHERE status='pending' AND due_at<=? ORDER BY due_at ASC",
            (now,))
        return [dict(r) for r in rows]

    def complete_followup(self, followup_id: int, status: str = "sent") -> None:
        self.execute("UPDATE followups SET status=? WHERE id=?", (status, followup_id))

    def pending_followups(self, lead_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM followups WHERE lead_id=? AND status='pending' ORDER BY due_at",
            (lead_id,))]

    # ------------------------------------------------------------------ events
    def log_event(self, actor: str, kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.execute(
            "INSERT INTO events (actor, kind, payload, created_at) VALUES (?,?,?,?)",
            (actor, kind, json.dumps(payload or {}, ensure_ascii=False)[:4000], _now()),
        )

    def events(self, actor: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if actor:
            rows = self.query("SELECT * FROM events WHERE actor=? ORDER BY id DESC LIMIT ?",
                              (actor, limit))
        else:
            rows = self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
            out.append(d)
        return out

    # ---------------------------------------------------------------------- kv
    def set_kv(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key)"
                     " DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))

    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except Exception:
            return default


def _normalise_lead(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Accept the aliases agents use so no caller has to care.

    ``business_name`` -> ``name`` and ``status`` -> ``stage``.  One place,
    documented, rather than eleven agents disagreeing about the column name.
    """
    out = dict(lead or {})
    if not out.get("name") and out.get("business_name"):
        out["name"] = out["business_name"]
    if not out.get("stage") and out.get("status"):
        out["stage"] = out["status"]
    return out


def _row_to_lead(row: sqlite3.Row) -> Dict[str, Any]:
    lead = dict(row)
    lead["business_name"] = lead.get("name", "")   # read-alias for callers
    lead["status"] = lead.get("stage", "")         # read-alias for callers
    for field in ("socials", "audit_issues"):
        raw = lead.get(field) or ""
        try:
            lead[field] = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            lead[field] = {} if field == "socials" else []
    return lead


def default_store() -> Store:
    """Process-wide store handle (SQLite connections are cheap, one is enough)."""
    store = getattr(_LOCAL, "store", None)
    if store is None:
        store = Store()
        _LOCAL.store = store
    return store
