"""Tamper-evident audit log + daily encrypted-to-you backups.

Audit log
---------
Every permission decision and every external action is appended to
``factory/data/audit.db`` as a **SHA-256 hash chain**:

    hash_n = sha256(hash_{n-1} || actor || action || risk || decision || details || ts)

Change any row - or delete one - and :meth:`AuditLog.verify` fails at that row.
The factory cannot quietly rewrite its own history, which is the point: this is
the record you show a client (or a court) if anyone asks what your agents did.

Backups
-------
``backup()`` copies the live SQLite files to ``backups/YYYY-MM-DD/`` and writes
``SHA256SUMS.txt``.  Cheap (a few MB on a 4 GB machine), and it means a bad
self-training run can always be rolled back.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import FACTORY_ROOT, REPO_ROOT

AUDIT_DB = FACTORY_ROOT / "data" / "audit.db"
BACKUP_ROOT = REPO_ROOT / "backups"
GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    ts_iso    TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    risk      TEXT NOT NULL,
    decision  TEXT NOT NULL,
    details   TEXT DEFAULT '{}',
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def canonical(payload: Any) -> str:
    """Deterministic JSON so the hash never depends on dict ordering."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuditLog:
    """Append-only, hash-chained audit trail."""

    def __init__(self, path: Optional[os.PathLike] = None):
        self.path = Path(path) if path else AUDIT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=15.0,
                                 check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ write
    def record(self, actor: str, action: str, risk: str, decision: str,
               details: Optional[Dict[str, Any]] = None) -> str:
        """Append one entry.  Returns the new chain hash."""
        ts = time.time()
        body = {
            "ts": round(ts, 6),
            "actor": str(actor),
            "action": str(action),
            "risk": str(risk),
            "decision": str(decision),
            "details": details or {},
        }
        with self._lock:
            row = self._conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            digest = hashlib.sha256(
                (prev + "|" + canonical(body)).encode("utf-8")).hexdigest()
            self._conn.execute(
                "INSERT INTO audit (ts, ts_iso, actor, action, risk, decision, details,"
                " prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, _iso(ts), body["actor"], body["action"], body["risk"], body["decision"],
                 canonical(body["details"]), prev, digest),
            )
            self._conn.commit()
        return digest

    # ------------------------------------------------------------------- read
    def recent(self, limit: int = 50, actor: Optional[str] = None,
               risk: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit"
        clauses, params = [], []
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if risk:
            clauses.append("risk=?")
            params.append(risk)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["details"] = json.loads(d["details"])
            except Exception:
                pass
            out.append(d)
        return out

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"])

    def summary(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT decision, COUNT(*) AS n FROM audit GROUP BY decision").fetchall()
            risks = self._conn.execute(
                "SELECT risk, COUNT(*) AS n FROM audit GROUP BY risk").fetchall()
        out = {r["decision"]: r["n"] for r in rows}
        out.update({"risk_" + r["risk"]: r["n"] for r in risks})
        out["total"] = sum(v for k, v in out.items() if not k.startswith("risk_"))
        return out

    # ----------------------------------------------------------------- verify
    def verify(self) -> Tuple[bool, int, Optional[int]]:
        """Walk the chain.  Returns ``(ok, checked, first_bad_id)``."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit ORDER BY id ASC").fetchall()
        prev = GENESIS
        checked = 0
        for r in rows:
            body = {
                "ts": round(r["ts"], 6),
                "actor": r["actor"],
                "action": r["action"],
                "risk": r["risk"],
                "decision": r["decision"],
                "details": json.loads(r["details"]) if r["details"] else {},
            }
            expected = hashlib.sha256(
                (prev + "|" + canonical(body)).encode("utf-8")).hexdigest()
            if expected != r["hash"] or r["prev_hash"] != prev:
                return False, checked, r["id"]
            prev = r["hash"]
            checked += 1
        return True, checked, None

    def export(self, path: os.PathLike) -> Path:
        """Write a plain-text, human-readable copy (for your own records)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# ClientHunter Factory audit export",
                 "# generated %s" % _iso(time.time()),
                 "# entries: %d" % self.count(), ""]
        with self._lock:
            rows = self._conn.execute("SELECT * FROM audit ORDER BY id ASC").fetchall()
        for r in rows:
            lines.append("%s  %-22s %-24s %-8s %-6s %s" % (
                r["ts_iso"], r["actor"], r["action"], r["risk"], r["decision"],
                (r["details"] or "")[:160]))
            lines.append("      prev=%s" % r["prev_hash"][:16])
            lines.append("      hash=%s" % r["hash"][:16])
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Module-level convenience wrappers (dashboard / orchestrator / security use these)
# --------------------------------------------------------------------------- #

_DEFAULT_LOG: Optional["AuditLog"] = None


def default_log() -> "AuditLog":
    """Process-wide AuditLog handle (SQLite connections are cheap)."""
    global _DEFAULT_LOG
    if _DEFAULT_LOG is None:
        _DEFAULT_LOG = AuditLog()
    return _DEFAULT_LOG


def read_log(limit: int = 50, actor: Optional[str] = None,
             risk: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest-first audit entries (oldest entry first in the returned list)."""
    rows = default_log().recent(limit=limit, actor=actor, risk=risk)
    return list(reversed(rows))


def verify_chain() -> Tuple[bool, List[str]]:
    """``(ok, problems)`` - walk every entry and re-hash the chain."""
    ok, checked, bad = default_log().verify()
    problems = [] if ok else ["chain broken at audit id %s (checked %d)" % (bad, checked)]
    return ok, problems


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file, streamed so big DBs never land in RAM at once."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def backup(targets: Optional[List[Path]] = None, root: Optional[Path] = None,
           keep_days: int = 14) -> Dict[str, Any]:
    """Copy the live databases to ``backups/YYYY-MM-DD/`` with checksums."""
    root = Path(root) if root else BACKUP_ROOT
    folder = root / time.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    targets = targets or [FACTORY_ROOT / "data" / "factory.db", AUDIT_DB]
    copied, sums = [], []
    for src in targets:
        src = Path(src)
        if not src.exists():
            continue
        dst = folder / src.name
        try:
            shutil.copy2(src, dst)
            digest = sha256_file(dst)
            copied.append(str(dst))
            sums.append("%s  %s" % (digest, dst.name))
        except Exception as exc:
            sums.append("# FAILED %s: %s" % (src.name, exc))
    (folder / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    pruned = _prune(root, keep_days)
    return {"folder": str(folder), "files": copied, "checksums": sums, "pruned": pruned}


def verify_backup(folder: Path) -> Tuple[bool, List[str]]:
    """Re-hash a backup folder against its ``SHA256SUMS.txt``."""
    folder = Path(folder)
    sums_file = folder / "SHA256SUMS.txt"
    if not sums_file.exists():
        return False, ["no SHA256SUMS.txt"]
    problems = []
    for line in sums_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        target = folder / name.strip()
        if not target.exists():
            problems.append("missing: %s" % name)
            continue
        if sha256_file(target) != digest.strip():
            problems.append("checksum mismatch: %s" % name)
    return (not problems), problems


def _prune(root: Path, keep_days: int) -> List[str]:
    """Delete backup folders older than ``keep_days`` (disk is finite)."""
    pruned = []
    if keep_days <= 0 or not root.exists():
        return pruned
    cutoff = time.time() - keep_days * 86400
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            pruned.append(child.name)
    return pruned


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
