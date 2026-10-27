"""WhatsApp tool - no API key, no cloud relay, no Node.js.

Tier 1: ``waeys`` - pure-Python port of Baileys (the WhatsApp Web protocol
    client).  Real package, Py3.10+, single runtime dependency (``websockets``).
    It speaks the WhatsApp Web protocol directly over WebSockets, so there is no
    browser to keep open and no Node.js in sight.  You pair once with a QR code
    or pairing code, and the session persists in the apartment sandbox.

Tier 2: CDP - drive WhatsApp Web inside your real Chrome (the browser tool), so
    it uses the session you are already logged into.  Used when ``waeys`` is not
    installed, or when you prefer the browser session.

Tier 3: outbox - in offline/demo mode messages are queued to the apartment's
    sandbox.  Nothing is sent, but the whole pipeline stays testable.

Hard limits, always on: 20 messages/day (ban protection), recipient allowlist,
and ``send_whatsapp`` is a HIGH-risk action that asks a human first.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

WA_SELECTORS = {
    "compose": 'div[contenteditable="true"][data-tab="10"]',
    "send_button": 'button[data-tab="11"]',
    "chat_list": 'div[aria-label="Chat list"]',
    "message_in": 'div.message-in',
    "search": 'div[contenteditable="true"][data-tab="3"]',
}


class WhatsAppTool:
    """Send and read WhatsApp messages."""

    #: WhatsApp caps message length at 65536; ours are intentionally short.
    MAX_CHARS = 1500

    def __init__(self, apartment):
        self.apartment = apartment
        self.backend = self._detect_backend()
        self._waeys = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop = None
        self.sent: List[Dict[str, Any]] = []
        self.queued: List[Dict[str, Any]] = []

    def _detect_backend(self) -> str:
        if self._probe_waeys():
            return "waeys"
        cfg = self.apartment.config.get("whatsapp", {}) or {}
        if cfg.get("force_browser"):
            return "cdp"
        return "cdp"     # CDP is always available as a fallback

    @staticmethod
    def _probe_waeys() -> bool:
        import importlib
        for name in ("WAeys", "waeys"):
            try:
                importlib.import_module(name)
                return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------ phones
    @staticmethod
    def normalise(phone: str) -> str:
        """``03001234567`` / ``+92 300 1234567`` -> ``923001234567``."""
        digits = re.sub(r"[^0-9]", "", phone or "")
        if digits.startswith("00"):
            digits = digits[2:]
        if digits.startswith("0"):
            digits = "92" + digits[1:]
        if not digits.startswith("92"):
            digits = "92" + digits
        return digits

    # ------------------------------------------------------------------- send
    def send_message(self, phone: str, text: str, dry_run: Optional[bool] = None
                     ) -> Dict[str, Any]:
        """Send one WhatsApp message.  Returns ``{"ok": ..., "backend": ...}``."""
        text = (text or "").strip()
        if not phone:
            return {"ok": False, "reason": "no phone number"}
        if not text:
            return {"ok": False, "reason": "empty message"}
        if len(text) > self.MAX_CHARS:
            text = text[: self.MAX_CHARS - 3] + "..."

        number = self.normalise(phone)
        decision = self.apartment.gate.check(
            self.apartment.NAME, "send_whatsapp",
            {"to": "+" + number, "chars": len(text), "preview": text[:80]})
        if not decision.allowed:
            self.apartment.log("whatsapp_blocked", to=number, reason=decision.reason)
            return {"ok": False, "reason": decision.reason, "risk": decision.risk}

        dry = self.apartment.offline if dry_run is None else dry_run
        if dry:
            return self._queue(number, text, "offline/dry-run")

        if self.backend == "waeys":
            result = self._send_waeys(number, text)
            if result.get("ok"):
                return result
            self.apartment.log("waeys_fallback", reason=result.get("reason"))
        result = self._send_cdp(number, text)
        if not result.get("ok"):
            return self._queue(number, text, result.get("reason", "send failed"))
        return result

    # ----------------------------------------------------------------- tiers
    def _send_waeys(self, number: str, text: str) -> Dict[str, Any]:  # pragma: no cover
        """Send through the pure-Python WhatsApp Web protocol client."""
        try:
            import asyncio
            import importlib
            module = None
            for name in ("WAeys", "waeys"):
                try:
                    module = importlib.import_module(name)
                    break
                except Exception:
                    continue
            if module is None:
                return {"ok": False, "reason": "waeys not importable"}

            defaults = importlib.import_module("WAeys.Defaults.index")
            auth_utils = importlib.import_module("WAeys.Utils.auth_utils")
            socket_mod = importlib.import_module("WAeys.Socket.socket")

            async def _run():
                config = defaults.default_connection_config()
                creds = auth_utils.init_auth_creds()
                store_dir = self.apartment.sandbox_path("wa_session")
                store_dir.mkdir(parents=True, exist_ok=True)
                config["auth"] = {"creds": creds, "keys": make_key_store(store_dir)}
                sock = socket_mod.make_socket(config)
                opened = asyncio.Event()

                async def on_update(update):
                    if update.get("qr"):
                        self.apartment.remember("wa_qr", update["qr"])
                        self.apartment.log("wa_qr", hint="scan in WhatsApp > Linked Devices")
                    if update.get("connection") == "open":
                        opened.set()

                sock["ev"].on("connection.update",
                              lambda u: asyncio.ensure_future(on_update(u)))
                try:
                    await asyncio.wait_for(opened.wait(), timeout=120)
                    await sock["sendMessage"]("%s@s.whatsapp.net" % number, {"text": text})
                    return {"ok": True, "backend": "waeys"}
                except asyncio.TimeoutError:
                    return {"ok": False, "reason": "pairing timeout - scan the QR first"}

            return asyncio.run(_run())
        except Exception as exc:
            return {"ok": False, "reason": "waeys error: %s" % exc}

    def _send_cdp(self, number: str, text: str) -> Dict[str, Any]:
        """Drive WhatsApp Web in the user's real Chrome session."""
        try:
            browser = self.apartment.tool("browser")
        except Exception as exc:
            return {"ok": False, "reason": "browser tool unavailable: %s" % exc}
        if not browser.is_running() and not browser.start():
            return {"ok": False, "reason": "chrome not running"}

        url = "https://web.whatsapp.com/send?phone=%s&text=%s" % (
            number, urllib.parse.quote(text))
        session = browser.new_tab()
        if session is None:
            return {"ok": False, "reason": "cannot open tab"}
        try:
            session.navigate(url, wait_s=6)
            if not session.wait_for(WA_SELECTORS["compose"], timeout_s=45):
                return {"ok": False,
                        "reason": "not logged in to WhatsApp Web (scan QR in the open tab)"}
            time.sleep(1.5)
            session.press("Enter")
            time.sleep(2.5)
            self.apartment.gate.enforce(self.apartment.NAME, "send_whatsapp",
                                        {"to": "+" + number, "via": "cdp"})
            record = {"to": number, "text": text, "at": time.time(), "backend": "cdp"}
            self.sent.append(record)
            self.apartment.log("whatsapp_sent", to=number, chars=len(text))
            return {"ok": True, "backend": "cdp", "to": number}
        except Exception as exc:
            return {"ok": False, "reason": "cdp error: %s" % exc}
        finally:
            session.close()

    def _queue(self, number: str, text: str, reason: str) -> Dict[str, Any]:
        record = {"to": number, "text": text, "at": time.time(), "reason": reason}
        self.queued.append(record)
        try:
            self.apartment.write_text(
                "outbox/whatsapp_%s.json" % time.strftime("%Y%m%d_%H%M%S"),
                json.dumps(record, ensure_ascii=False, indent=1))
            self.apartment.write_text(
                "outbox/whatsapp_log.txt",
                "[%s] -> +%s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), number,
                                        text.replace("\n", " ")[:90]),
                append=True)
        except Exception:
            pass
        self.apartment.log("whatsapp_queued", to=number, reason=reason)
        return {"ok": True, "queued": True, "reason": reason, "backend": "outbox"}

    # ------------------------------------------------------------------- read
    def read_messages(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Read recent chats.  CDP reads the open chat; waeys would stream events."""
        if self.apartment.offline:
            return [{"from": item["to"], "text": item["text"], "ts": item["at"],
                     "direction": "out", "queued": True} for item in self.queued[-limit:]]
        try:
            browser = self.apartment.tool("browser")
            if not browser.is_running():
                return []
            session = browser.attach_tab("WhatsApp") or browser.new_tab("https://web.whatsapp.com")
            if session is None:
                return []
            script = (
                "(function(){var out=[];"
                "document.querySelectorAll('div.message-in,div.message-out')"
                ".forEach(function(el){"
                "var txt=(el.innerText||'').split('\\n');"
                "out.push({direction: el.className.indexOf('message-in')>=0?'in':'out',"
                "text: txt.slice(0,-1).join(' ').slice(0,500), ts: txt[txt.length-1]||''});});"
                "return out.slice(-%d);})()" % int(limit))
            messages = session.evaluate(script) or []
            return messages
        except Exception as exc:
            self.apartment.log("whatsapp_read_failed", error=str(exc))
            return []

    def list_chats(self, limit: int = 20) -> List[Dict[str, str]]:
        """Names of recent chats, for choosing who to reply to."""
        if self.apartment.offline:
            return []
        try:
            browser = self.apartment.tool("browser")
            session = browser.attach_tab("WhatsApp")
            if session is None:
                return []
            script = (
                "(function(){var out=[];"
                "document.querySelectorAll('div[role=listitem]').forEach(function(el){"
                "var t=(el.innerText||'').split('\\n');"
                "if(t[0]) out.push({name:t[0], preview:t[1]||''});});"
                "return out.slice(0,%d);})()" % int(limit))
            return session.evaluate(script) or []
        except Exception:
            return []

    def get_contact_info(self, phone: str) -> Dict[str, Any]:
        number = self.normalise(phone)
        return {"phone": "+" + number, "jid": "%s@s.whatsapp.net" % number,
                "whatsapp_url": "https://wa.me/%s" % number}

    def pairing_qr(self) -> Optional[str]:
        """Last QR offered by ``waeys`` (scan in WhatsApp > Linked Devices)."""
        return self.apartment.recall("wa_qr")

    def info(self) -> Dict[str, Any]:
        return {"backend": self.backend, "waeys_installed": self._probe_waeys(),
                "sent": len(self.sent), "queued": len(self.queued),
                "offline": self.apartment.offline}


def make_key_store(folder: Path):  # pragma: no cover - used by the waeys path
    """Minimal file-backed key store for waeys session persistence."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cache: Dict[str, Any] = {}
    index = folder / "keys.json"
    if index.exists():
        try:
            cache.update(json.loads(index.read_text(encoding="utf-8")))
        except Exception:
            pass

    def get(key: str, default=None):
        return cache.get(key, default)

    def set(key: str, value: Any) -> None:
        cache[key] = value
        try:
            index.write_text(json.dumps(cache, default=str), encoding="utf-8")
        except Exception:
            pass

    class _Store:
        pass

    store = _Store()
    store.get = get
    store.set = set
    return store


def build(apartment) -> WhatsAppTool:
    return WhatsAppTool(apartment)
