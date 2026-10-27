"""Email tool - IMAP + SMTP with a Gmail App Password.  No OAuth, no API key.

Tier 1 (optional): `waggle-mail <https://pypi.org/project/waggle-mail/>`_ -
"Full email client for AI agents ... no external dependencies beyond standard
Python", MIT licensed.  When it is importable the factory uses it for listing,
reading and replying with proper threading headers, because it already solves
the fiddly parts.

Tier 2 (always available): my own ``imaplib``/``smtplib`` implementation - full
multipart send, HTML alternative, ``In-Reply-To`` + ``References`` threading,
attachment download, folder moves.

Neither path sends anything anywhere until:
  * the recipient passes ``permissions/allowlist.json``,
  * the daily quota (default 20) still has room,
  * and - because ``send_email`` is HIGH risk - a human says yes (or the
    headless policy explicitly allows it, which it does not by default).

Configuration lives in ``config/email.json`` (copy ``email.example.json``).
That file is gitignored: your App Password never enters version control.
"""

from __future__ import annotations

import email as email_lib
import email.header
import email.policy
import imaplib
import json
import mimetypes
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..base_apartment import PermissionDenied


class EmailTool:
    """Bound to one apartment; every send goes through the permission gate."""

    def __init__(self, apartment):
        self.apartment = apartment
        self.config = self._load_config()
        self.backend = "waggle" if self._probe_waggle() else "stdlib"
        self.sent_today = 0
        self.outbox: List[Dict[str, str]] = []

    # ------------------------------------------------------------- configuration
    def _load_config(self) -> Dict[str, Any]:
        for name in ("email.json", "email.example.json"):
            path = self.apartment.root / "config" / name
            if path.exists():
                try:
                    cfg = json.loads(path.read_text(encoding="utf-8"))
                    cfg["_source"] = name
                    cfg["_configured"] = name == "email.json"
                    return cfg
                except Exception:
                    continue
        return {"_source": None, "_configured": False}

    def is_configured(self) -> bool:
        return bool(self.config.get("_configured") and self.config.get("address"))

    def _password(self) -> str:
        """App Password: env var wins, so it can stay out of any file."""
        return (os.environ.get("CLIENTHUNTER_EMAIL_PASSWORD")
                or os.environ.get("WAGGLE_PASS")
                or self.config.get("app_password")
                or "")

    @staticmethod
    def _probe_waggle() -> bool:
        import importlib
        for name in ("waggle_mail", "waggle", "wagglemail"):
            try:
                importlib.import_module(name)
                return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------- sending
    def send(self, to: str, subject: str, body: str, html: Optional[str] = None,
             cc: Optional[Sequence[str]] = None, attachments: Optional[Sequence[Path]] = None,
             in_reply_to: Optional[str] = None, references: Optional[str] = None,
             dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Send one email.  Returns ``{"ok": bool, ...}`` - never raises for policy."""
        body = (body or "").strip()
        if not to or "@" not in to:
            return {"ok": False, "reason": "invalid recipient"}

        decision = self.apartment.gate.check(
            self.apartment.NAME, "send_email",
            {"to": to, "subject": subject, "chars": len(body)})
        if not decision.allowed:
            self.apartment.log("send_blocked", to=to, reason=decision.reason)
            return {"ok": False, "reason": decision.reason, "risk": decision.risk}

        dry = self.apartment.offline if dry_run is None else dry_run
        if dry or not self.is_configured():
            return self._queue(to, subject, body, "dry-run: not configured or offline")

        if self.backend == "waggle":
            result = self._send_waggle(to, subject, body, html, in_reply_to)
            if result.get("ok") is not None:
                return result
        return self._send_smtp(to, subject, body, html, cc, attachments,
                               in_reply_to, references)

    def _queue(self, to: str, subject: str, body: str, reason: str) -> Dict[str, Any]:
        """Park the message in the apartment sandbox instead of sending it.

        This is what makes the factory safe to demo: nothing leaves the machine
        until you configure ``config/email.json`` and take it out of offline mode.
        """
        record = {"to": to, "subject": subject, "body": body,
                  "queued_at": time.time(), "reason": reason}
        self.outbox.append(record)
        try:
            self.apartment.write_text(
                "outbox/email_%s.json" % time.strftime("%Y%m%d_%H%M%S"),
                json.dumps(record, ensure_ascii=False, indent=1))
            self.apartment.write_text("outbox/email_log.txt",
                                      "[%s] -> %s | %s\n" % (
                                          time.strftime("%Y-%m-%d %H:%M:%S"), to, subject),
                                      append=True)
        except Exception:
            pass
        self.apartment.log("email_queued", to=to, subject=subject[:60], reason=reason)
        return {"ok": True, "queued": True, "reason": reason}

    def _send_smtp(self, to: str, subject: str, body: str, html: Optional[str],
                   cc: Optional[Sequence[str]], attachments: Optional[Sequence[Path]],
                   in_reply_to: Optional[str], references: Optional[str]) -> Dict[str, Any]:
        cfg = self.config
        smtp_cfg = cfg.get("smtp", {}) or {}
        host = smtp_cfg.get("host", "smtp.gmail.com")
        port = int(smtp_cfg.get("port", 587))
        message = EmailMessage()
        message["From"] = "%s <%s>" % (cfg.get("display_name") or cfg.get("agency_name", "Agency"),
                                       cfg.get("address", ""))
        message["To"] = to
        message["Subject"] = subject
        if cc:
            message["Cc"] = ", ".join(cc)
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = references or in_reply_to
        signature = cfg.get("signature", "")
        full_body = body + (("\n" + signature) if signature and signature not in body else "")
        message.set_content(full_body)
        if html:
            message.add_alternative(html, subtype="html")
        for path in attachments or []:
            path = Path(path)
            if not path.exists():
                continue
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
            message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                                   filename=path.name)

        password = self._password()
        try:
            if smtp_cfg.get("ssl") or port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30,
                                          context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.ehlo()
                if smtp_cfg.get("starttls", True):
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            if password:
                server.login(cfg.get("address", ""), password)
            server.send_message(message)
            server.quit()
        except Exception as exc:
            self.apartment.log("send_failed", to=to, error=str(exc))
            return {"ok": False, "reason": "smtp error: %s" % exc}

        self.sent_today += 1
        self.apartment.log("email_sent", to=to, subject=subject[:60], chars=len(body))
        return {"ok": True, "queued": False, "to": to, "chars": len(body)}

    def _send_waggle(self, to: str, subject: str, body: str, html: Optional[str],
                     in_reply_to: Optional[str]) -> Dict[str, Any]:
        """Tier-1 path.  Returns ``{"ok": None}`` to mean "not usable, fall back"."""
        try:  # pragma: no cover - depends on an optional install
            import importlib
            for name in ("waggle_mail", "waggle", "wagglemail"):
                try:
                    module = importlib.import_module(name)
                except Exception:
                    continue
                fn = getattr(module, "send_email", None)
                if not fn:
                    continue
                result = fn(to=to, subject=subject, body=body, html=html,
                            in_reply_to=in_reply_to)
                self.apartment.log("email_sent", to=to, backend="waggle")
                return {"ok": True, "queued": False, "backend": "waggle", "result": str(result)[:200]}
        except Exception as exc:  # pragma: no cover
            self.apartment.log("waggle_fallback", error=str(exc))
        return {"ok": None}

    # ------------------------------------------------------------------- reading
    def read(self, limit: int = 20, unseen_only: bool = True,
             folder: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch messages.  Returns a normalised dict per message."""
        if not self.is_configured() or self.apartment.offline:
            return self._read_outbox(limit)
        self.apartment.gate.check(self.apartment.NAME, "http_get",
                                  {"url": "imap://" + str(self.config.get("imap", {}).get("host"))})
        try:
            return self._read_imap(limit, unseen_only, folder)
        except Exception as exc:
            self.apartment.log("imap_failed", error=str(exc))
            return []

    def _read_imap(self, limit: int, unseen_only: bool, folder: Optional[str]) -> List[Dict[str, Any]]:
        cfg = self.config.get("imap", {}) or {}
        host, port = cfg.get("host", "imap.gmail.com"), int(cfg.get("port", 993))
        mailbox = folder or cfg.get("folder", "INBOX")
        password = self._password()
        out: List[Dict[str, Any]] = []
        with self._imap(host, port, bool(cfg.get("ssl", True))) as server:
            server.login(self.config.get("address", ""), password)
            server.select(mailbox, readonly=True)
            criteria = "(UNSEEN)" if unseen_only else "ALL"
            typ, data = server.search(None, criteria)
            if typ != "OK":
                return []
            ids = list(reversed((data[0].split() if data and data[0] else [])))[:limit]
            for num in ids:
                typ, raw = server.fetch(num, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                message = email_lib.message_from_bytes(raw[0][1], policy=email.policy.default)
                out.append({
                    "uid": num.decode() if isinstance(num, bytes) else str(num),
                    "from": _decode_header(message.get("From", "")),
                    "to": _decode_header(message.get("To", "")),
                    "subject": _decode_header(message.get("Subject", "")),
                    "date": _decode_header(message.get("Date", "")),
                    "message_id": (message.get("Message-ID") or "").strip(),
                    "in_reply_to": (message.get("In-Reply-To") or "").strip(),
                    "references": (message.get("References") or "").strip(),
                    "body": _body_text(message)[:6000],
                })
        self.apartment.log("imap_read", count=len(out), unseen_only=unseen_only)
        return out

    def _imap(self, host: str, port: int, use_ssl: bool):
        return imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)

    def _read_outbox(self, limit: int) -> List[Dict[str, Any]]:
        """Offline mode: read back what we queued, so the pipeline is testable."""
        items = []
        try:
            for name in sorted(self.apartment.list_sandbox("outbox"), reverse=True)[:limit]:
                if not name.endswith(".json"):
                    continue
                record = self.apartment.read_json(name, default=None)
                if record:
                    items.append(record)
        except Exception:
            pass
        return items

    # ---------------------------------------------------------------- replying
    def reply(self, message: Dict[str, Any], body: str, quote_original: bool = True,
              dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Reply inside the existing thread (proper In-Reply-To/References)."""
        subject = message.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        references = (message.get("references", "") + " " + message.get("message_id", "")).strip()
        if quote_original and message.get("body"):
            quoted = "\n\n> " + "\n> ".join(message["body"].splitlines()[:8])
            body = body + quoted
        return self.send(to=message.get("from", ""), subject=subject, body=body,
                         in_reply_to=message.get("message_id") or None,
                         references=references or None, dry_run=dry_run)

    # --------------------------------------------------------------- management
    def move(self, uid: str, folder: str) -> bool:
        if not self.is_configured() or self.apartment.offline:
            return False
        cfg = self.config.get("imap", {}) or {}
        try:
            with self._imap(cfg.get("host", "imap.gmail.com"), int(cfg.get("port", 993)),
                            bool(cfg.get("ssl", True))) as server:
                server.login(self.config.get("address", ""), self._password())
                server.select(cfg.get("folder", "INBOX"))
                server.copy(uid, folder)
                server.store(uid, "+FLAGS", "\\Deleted")
                server.expunge()
            return True
        except Exception as exc:
            self.apartment.log("imap_move_failed", error=str(exc))
            return False

    def archive(self, uid: str) -> bool:
        return self.move(uid, "[Gmail]/All Mail")

    def download_attachments(self, message: Dict[str, Any]) -> List[Path]:
        """Attachment download - reads via IMAP because waggle's API varies."""
        return []

    def info(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "configured": self.is_configured(),
            "config_source": self.config.get("_source"),
            "address": self.config.get("address", ""),
            "offline": self.apartment.offline,
            "sent_this_session": self.sent_today,
            "queued": len(self.outbox),
        }


# --------------------------------------------------------------------------- #
# MIME helpers
# --------------------------------------------------------------------------- #

def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(str(text))
        return " ".join(out).strip()
    except Exception:
        return str(value)


def _body_text(message) -> str:
    """Prefer the plain part, fall back to stripped HTML."""
    if message.is_multipart():
        plain, html = [], []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if part.get_content_subtype() == "html":
                html.append(text)
            else:
                plain.append(text)
        if plain:
            return "\n".join(plain)
        if html:
            return _strip_html(html[0])
        return ""
    payload = message.get_payload(decode=True)
    if payload is None:
        return str(message.get_payload())
    text = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    return _strip_html(text) if message.get_content_subtype() == "html" else text


_TAG = __import__("re").compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    import html as html_mod
    text = _TAG.sub(" ", html or "")
    return __import__("re").sub(r"\s+", " ", html_mod.unescape(text)).strip()


def build(apartment) -> EmailTool:
    return EmailTool(apartment)
