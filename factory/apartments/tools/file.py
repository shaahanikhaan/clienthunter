"""Sandboxed file tool.

An apartment may only read and write inside its own ``sandbox/`` folder.
Absolute paths, ``..`` traversal and symlinks that point outside are all
refused.  Nothing here can reach another apartment's home.

Extra safety: writes are atomic (temp file + ``os.replace``) so a crash midway
can never leave a half-written lead file behind.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..base_apartment import PermissionDenied


class FileTool:
    """Filesystem access, jailed to ``apartment.home / 'sandbox'``."""

    def __init__(self, apartment):
        self.apartment = apartment
        self.root = (apartment.home / "sandbox").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- path safety
    def path(self, relpath: str) -> Path:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise PermissionDenied("apt_%s: absolute path or traversal refused: %r"
                                   % (self.apartment.NAME, relpath))
        target = (self.root / rel).resolve()
        if target != self.root and not str(target).startswith(str(self.root) + os.sep):
            raise PermissionDenied("apt_%s: path escapes sandbox: %r"
                                   % (self.apartment.NAME, relpath))
        # Symlink check: the resolved parent chain must stay inside the root.
        for parent in target.parents:
            if parent.is_symlink():
                raise PermissionDenied("apt_%s: symlink in path refused: %r"
                                       % (self.apartment.NAME, relpath))
        return target

    def _check(self, perm: str) -> None:
        if not self.apartment.can(perm):
            raise PermissionDenied("apt_%s: file permission %r denied"
                                   % (self.apartment.NAME, perm))

    # ----------------------------------------------------------------- reading
    def read(self, relpath: str, default: Optional[str] = None) -> Optional[str]:
        self._check("file_read")
        target = self.path(relpath)
        if not target.exists() or not target.is_file():
            if default is None:
                raise FileNotFoundError(target)
            return default
        return target.read_text(encoding="utf-8", errors="replace")

    def read_bytes(self, relpath: str) -> bytes:
        self._check("file_read")
        return self.path(relpath).read_bytes()

    def read_json(self, relpath: str, default: Any = None) -> Any:
        raw = self.read(relpath, default=None)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def read_csv(self, relpath: str) -> List[Dict[str, str]]:
        """Read a CSV into dicts.  Handles the UTF-8 BOM Excel adds on Windows."""
        raw = self.read(relpath, default="")
        if not raw.strip():
            return []
        return list(csv.DictReader(io.StringIO(raw.lstrip("﻿"))))

    def exists(self, relpath: str) -> bool:
        return self.path(relpath).exists()

    # ----------------------------------------------------------------- writing
    def write(self, relpath: str, text: str, append: bool = False) -> Path:
        self._check("file_write")
        target = self.path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8", newline="") as fh:
            fh.write(text)
        return target

    def write_atomic(self, relpath: str, text: str) -> Path:
        self._check("file_write")
        target = self.path(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return target

    def write_json(self, relpath: str, obj: Any) -> Path:
        return self.write_atomic(relpath, json.dumps(obj, ensure_ascii=False, indent=1,
                                                     default=str))

    def write_csv(self, relpath: str, rows: Iterable[Dict[str, Any]],
                  fieldnames: Optional[List[str]] = None) -> Path:
        rows = list(rows)
        if not rows:
            return self.write_atomic(relpath, "")
        if not fieldnames:
            fieldnames = []
            for row in rows:
                for key in row:
                    if key not in fieldnames:
                        fieldnames.append(key)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _flatten(v) for k, v in row.items()})
        return self.write_atomic(relpath, buf.getvalue())

    def append_csv(self, relpath: str, rows: Iterable[Dict[str, Any]],
                   fieldnames: Optional[List[str]] = None) -> Path:
        """Append rows, creating the file with a header when it does not exist."""
        rows = list(rows)
        if not rows:
            return self.path(relpath)
        if not fieldnames:
            fieldnames = list(rows[0].keys())
        new_file = not self.path(relpath).exists()
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: _flatten(v) for k, v in row.items()})
        return self.write(relpath, buf.getvalue(), append=True)

    def delete(self, relpath: str) -> bool:
        self._check("file_write")
        target = self.path(relpath)
        if target.exists() and target.is_file():
            target.unlink()
            return True
        return False

    # ------------------------------------------------------------------ listing
    def list(self, sub: str = "", pattern: str = "*") -> List[str]:
        base = self.path(sub) if sub else self.root
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob(pattern) if p.is_file())

    def mkdir(self, sub: str) -> Path:
        self._check("file_write")
        target = self.path(sub)
        target.mkdir(parents=True, exist_ok=True)
        return target


def _flatten(value: Any) -> Any:
    """CSV cells must be scalars - serialise containers as JSON."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def build(apartment) -> FileTool:
    return FileTool(apartment)
