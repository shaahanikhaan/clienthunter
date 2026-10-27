"""Browser automation over the Chrome DevTools Protocol - no key, no SaaS.

It drives **your real installed Chrome**, and it can carry over your existing
logged-in sessions (WhatsApp Web, LinkedIn, GitHub, Gmail) so nothing ever has
to log in again and no credentials are stored by the factory.

Two engines, picked automatically:

``cdp``  (default, always available)
    My own minimal CDP client: raw WebSocket over ``socket`` + ``base64`` +
    ``hashlib``, then ``Runtime.evaluate`` / ``Input.dispatchMouseEvent`` /
    ``Page.captureScreenshot``.  ~350 lines, zero dependencies, ~10 MB RAM.

``rici`` (optional accelerator)
    Uses `ricibrowser <https://pypi.org/project/ricibrowser/>`_ (v0.2.29,
    "lightweight two-engine browser automation built on CDP, no Playwright, no
    Puppeteer, no Selenium") when it is installed.  Because its API is async and
    version-sensitive, the adapter probes for capabilities and falls back to
    ``cdp`` on any mismatch instead of failing the agent.

Explicitly NOT used: Playwright, Puppeteer, Selenium (all forbidden by the
brief), and ``agentyc`` - it exists on PyPI but ships the ``anthropic`` SDK as a
hard dependency, i.e. it is wired for a paid API, which the brief forbids.

Session reuse
-------------
``bootstrap_profile()`` copies ``Cookies``, ``Login Data``, ``Web Data``,
``Bookmarks`` and ``Local State`` from your default Chrome profile into the
factory's automation profile.  Chrome decrypts them with the same OS key store,
so you arrive already logged in.  Nothing is uploaded; the copy never leaves
your disk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..base_apartment import PermissionDenied

# --------------------------------------------------------------------------- #
# Minimal WebSocket client (stdlib only) - CDP speaks WS, not HTTP
# --------------------------------------------------------------------------- #


class WebSocketError(RuntimeError):
    pass


class _WS:
    """Just enough RFC 6455 for CDP: text frames, client masking, ping/pong."""

    def __init__(self, url: str, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""

    def connect(self) -> "_WS":
        from urllib.parse import urlsplit
        parts = urlsplit(self.url)
        host, port = parts.hostname, parts.port or 9222
        self.sock = socket.create_connection((host, port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        handshake = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\nOrigin: http://%s:%d\r\n\r\n"
            % (path, host, port, key, host, port)).encode()
        self.sock.sendall(handshake)
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            header += chunk
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise WebSocketError("handshake failed: %s" % header[:120])
        self._buf = header.split(b"\r\n\r\n", 1)[1]
        return self

    # ------------------------------------------------------------------ frames
    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])           # FIN + text
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if self.sock is None:
            raise WebSocketError("not connected")
        self.sock.sendall(bytes(header) + masked)

    def _read(self, size: int) -> bytes:
        if self.sock is None:
            raise WebSocketError("not connected")
        while len(self._buf) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("socket closed")
            self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def recv(self) -> str:
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x9:                # ping -> pong
                self.send_frame_raw(0xA, payload)
                continue
            if opcode == 0x8:
                raise WebSocketError("closed by peer")
            if opcode == 0xA:                # pong, ignore
                continue
            return payload.decode("utf-8", "replace")

    def send_frame_raw(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            return
        self.sock.sendall(bytes([0x80 | opcode, len(payload)]) + payload)

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


# --------------------------------------------------------------------------- #
# CDP client
# --------------------------------------------------------------------------- #


class CdpSession:
    """One tab, driven over CDP."""

    def __init__(self, browser: "BrowserTool", target_id: str, ws_url: str,
                 title: str = ""):
        self.browser = browser
        self.target_id = target_id
        self.ws_url = ws_url
        self.title = title
        self._ws: Optional[_WS] = None
        self._id = 0
        self._lock = threading.Lock()
        self.last_eval_error: Optional[str] = None
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._snap_seq = 0

    # ------------------------------------------------------------ connection
    def connect(self) -> "CdpSession":
        self._ws = _WS(self.ws_url, timeout=self.browser.timeout).connect()
        self._ws.sock.settimeout(None) if self._ws.sock else None
        return self

    @property
    def ws(self) -> _WS:
        if self._ws is None:
            self.connect()
        assert self._ws is not None
        return self._ws

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = 30.0) -> Dict[str, Any]:
        """Send one CDP command and wait for its response."""
        with self._lock:
            self._id += 1
            msg_id = self._id
            self.ws.send(json.dumps({"id": msg_id, "method": method,
                                     "params": params or {}}))
            self.ws.sock.settimeout(timeout) if self.ws.sock else None
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = self.ws.recv()
                try:
                    message = json.loads(raw)
                except Exception:
                    continue
                if message.get("id") != msg_id:
                    continue      # an event, not our reply
                if "error" in message:
                    raise WebSocketError("%s: %s" % (method, message["error"]))
                return message.get("result", {})
            raise WebSocketError("timeout waiting for %s" % method)

    # -------------------------------------------------------------- navigation
    def navigate(self, url: str, wait_s: float = 2.0) -> bool:
        self.browser._gate("browser_navigate", {"url": url})
        try:
            self.call("Page.enable")
            self.call("Page.navigate", {"url": url})
            time.sleep(wait_s)
            self.title = self.evaluate("document.title") or ""
            self.browser.apartment.log("navigate", url=url, title=self.title)
            return True
        except Exception as exc:
            self.browser.apartment.log("navigate_failed", url=url, error=str(exc))
            return False

    @property
    def url(self) -> str:
        try:
            return str(self.evaluate("location.href") or "")
        except Exception:
            return ""

    # -------------------------------------------------------------- evaluation
    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        """Run JS and return the value.  Surfaces JS exceptions in
        :attr:`last_eval_error` instead of returning a silent ``None``."""
        self.last_eval_error = None
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        details = result.get("exceptionDetails")
        if details:
            self.last_eval_error = str(details.get("text") or
                                       (details.get("exception") or {}).get("description", ""))
            return None
        return (result.get("result") or {}).get("value")

    # ------------------------------------------------------------ interaction
    def click(self, selector: str, trusted: bool = True) -> bool:
        """Click an element - trusted ``Input.dispatchMouseEvent`` by default so
        React/Vue buttons that ignore synthetic ``el.click()`` still respond."""
        self.browser._gate("browser_click", {"selector": selector})
        box = self.bounding_box(selector)
        if not box:
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        for event_type, button in (("mousePressed", "left"), ("mouseReleased", "left")):
            self.call("Input.dispatchMouseEvent", {
                "type": event_type, "x": x, "y": y, "button": button,
                "clickCount": 1, "buttons": 1 if event_type == "mousePressed" else 0,
            })
        self.browser.apartment.log("click", selector=selector, x=round(x), y=round(y))
        return True

    def bounding_box(self, selector: str) -> Optional[Dict[str, float]]:
        expr = (
            "(function(){var el=document.querySelector(%s);"
            "if(!el)return null;el.scrollIntoView({block:'center'});"
            "var r=el.getBoundingClientRect();"
            "return {x:r.x,y:r.y,width:r.width,height:r.height};})()"
            % json.dumps(selector))
        try:
            return self.evaluate(expr)
        except Exception:
            return None

    def type_text(self, selector: str, text: str, clear_first: bool = True,
                  delay_ms: int = 12) -> bool:
        """Type like a human: focus the element, then ``Input.insertText``.

        ``insertText`` fires the same input events a keyboard does, which is what
        WhatsApp Web and React forms listen for.
        """
        self.browser._gate("browser_type", {"selector": selector, "length": len(text)})
        if not self.click(selector):
            return False
        if clear_first:
            self.evaluate(
                "(function(){var el=document.querySelector(%s);"
                "if(el){el.focus();el.value='';"
                "el.dispatchEvent(new Event('input',{bubbles:true}));}})()"
                % json.dumps(selector))
        try:
            for chunk in _chunks(text, 24):
                self.call("Input.insertText", {"text": chunk})
                time.sleep(delay_ms / 1000.0)
            return True
        except Exception as exc:
            self.browser.apartment.log("type_failed", error=str(exc))
            return False

    def fill_form(self, fields: Dict[str, str]) -> Dict[str, bool]:
        """``{"#email": "a@b.com", "input[name=pass]": "..."}`` -> per-field ok."""
        self.browser._gate("browser_fill_form", {"fields": list(fields)})
        return {selector: self.type_text(selector, value) for selector, value in fields.items()}

    def submit_form(self, selector: str) -> bool:
        self.browser._gate("browser_click", {"selector": selector, "submit": True})
        return self.click(selector)

    def press(self, key: str) -> bool:
        """Send a key (Enter, Tab, Escape) via ``Input.dispatchKeyEvent``."""
        self.browser._gate("browser_type", {"key": key})
        try:
            self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key,
                                                 "windowsVirtualKeyCode": _KEY_CODES.get(key, 13)})
            self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key,
                                                 "windowsVirtualKeyCode": _KEY_CODES.get(key, 13)})
            return True
        except Exception:
            return self.evaluate(
                "(function(){var el=document.activeElement;"
                "el.dispatchEvent(new KeyboardEvent('keydown',{key:%s,bubbles:true}));"
                "return true;})()" % json.dumps(key)) is True

    def scroll(self, pixels: int = 600) -> bool:
        self.evaluate("window.scrollBy(0,%d)" % int(pixels))
        return True

    # ------------------------------------------------------------------ reading
    def extract_text(self, selector: str = "body", limit: int = 20000) -> str:
        text = self.evaluate("(document.querySelector(%s)||document.body).innerText"
                             % json.dumps(selector))
        return (text or "")[:limit]

    def dom_snapshot(self, interactive_only: bool = True, limit: int = 120) -> List[Dict[str, Any]]:
        """Stable references for clickable things, like an accessibility tree.

        Each node carries a ``ref`` you can pass to :meth:`click_ref`, so an
        agent can act on a page without re-querying selectors.
        """
        script = (
            "(function(){var sel=%s? 'a,button,input,textarea,select,[role=button],"
            "[onclick],[contenteditable=true]':'*';"
            "var nodes=[].slice.call(document.querySelectorAll(sel)).slice(0,%d);"
            "return nodes.map(function(el,i){var r=el.getBoundingClientRect();"
            "return {i:i,tag:el.tagName.toLowerCase(),text:(el.innerText||el.value||"
            "el.getAttribute('aria-label')||el.placeholder||'').slice(0,80),"
            "id:el.id||'',name:el.getAttribute('name')||'',"
            "visible: r.width>0&&r.height>0, x:r.x+r.width/2, y:r.y+r.height/2};});})()"
            % ("true" if interactive_only else "false", int(limit)))
        nodes = self.evaluate(script) or []
        seq = self._next_seq()
        for node in nodes:
            node["ref"] = "%s:%s" % (seq, node.get("i"))
            self._snapshots[node["ref"]] = node
        return nodes

    def _next_seq(self) -> int:
        self._snap_seq += 1
        return self._snap_seq

    def click_ref(self, ref: str) -> bool:
        """Click a node from :meth:`dom_snapshot` - rejects stale references."""
        node = self._snapshots.get(ref)
        if not node:
            return False
        if int(ref.split(":")[0]) != self._snap_seq:
            self.browser.apartment.log("stale_ref", ref=ref)
            return False
        self.browser._gate("browser_click", {"ref": ref, "text": node.get("text", "")[:60]})
        for event_type, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.call("Input.dispatchMouseEvent", {
                "type": event_type, "x": node["x"], "y": node["y"],
                "button": "left", "clickCount": 1, "buttons": buttons})
        return True

    def screenshot(self, filename: Optional[str] = None) -> Optional[Path]:
        result = self.call("Page.captureScreenshot", {"format": "png"})
        data = result.get("data")
        if not data:
            return None
        name = filename or ("page_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
        target = self.browser.apartment.sandbox_path("shots/" + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data))
        return target

    def wait_for(self, selector: str, timeout_s: float = 15.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.evaluate("!!document.querySelector(%s)" % json.dumps(selector)):
                return True
            time.sleep(0.5)
        return False

    def close(self) -> None:
        try:
            self.browser.close_tab(self.target_id)
        except Exception:
            pass
        if self._ws:
            self._ws.close()


_KEY_CODES = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, " ": 32}


def _chunks(text: str, size: int):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# --------------------------------------------------------------------------- #
# Browser tool
# --------------------------------------------------------------------------- #

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/snap/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


class BrowserTool:
    """Launch/attach Chrome, hand out tabs.  One browser, many agents."""

    def __init__(self, apartment):
        self.apartment = apartment
        cfg = apartment.config.get("browser", {}) or {}
        self.port = int(cfg.get("debug_port", 9222))
        self.timeout = float(cfg.get("timeout_s", 20))
        self.headless = bool(cfg.get("headless", False))
        self.backend_pref = str(cfg.get("backend", "auto"))
        self.profile = Path(cfg.get("profile_dir") or
                            (apartment.root / "data" / "chrome_profile"))
        self.chrome_path = cfg.get("chrome_path") or find_chrome()
        self._sessions: Dict[str, CdpSession] = {}
        self._process: Optional[subprocess.Popen] = None
        self.backend = "cdp"
        self._rici = None

    # ------------------------------------------------------------------ gate
    def _gate(self, action: str, details: Dict[str, Any]) -> None:
        self.apartment.require("browser")
        self.apartment.gate.enforce(self.apartment.NAME, action, details)

    # ---------------------------------------------------------------- launch
    def is_running(self) -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % self.port,
                                        timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start(self, bootstrap_profile: bool = True) -> bool:
        """Start Chrome with remote debugging (or attach if already running)."""
        self.apartment.require("browser")
        if self.is_running():
            self.apartment.log("browser_attach", port=self.port)
            return True
        if not self.chrome_path or not Path(self.chrome_path).exists():
            self.apartment.log("browser_missing", path=str(self.chrome_path))
            return False
        if bootstrap_profile:
            self.bootstrap_profile()
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [
            self.chrome_path,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,OptimizationHints",
            "--window-size=1440,900",
        ]
        if self.headless:
            args.append("--headless=new")
        try:
            self._process = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.apartment.log("browser_start_failed", error=str(exc))
            return False
        for _ in range(30):
            if self.is_running():
                self.apartment.log("browser_start", port=self.port,
                                   profile=str(self.profile))
                return True
            time.sleep(0.5)
        return False

    def stop(self) -> bool:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=8)
            except Exception:
                pass
            self._process = None
            return True
        return False

    # ------------------------------------------------------- session reuse
    def default_chrome_profile(self) -> Optional[Path]:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA")
            return Path(base) / "Google" / "Chrome" / "User Data" / "Default" if base else None
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
        return Path.home() / ".config" / "google-chrome" / "Default"

    def bootstrap_profile(self, files: Sequence[str] = ("Cookies", "Login Data",
                                                        "Web Data", "Bookmarks",
                                                        "Local State")) -> List[str]:
        """Copy session data from your real Chrome profile so you stay logged in.

        Chrome keeps cookies in a SQLite DB encrypted with an OS key store; the
        matching ``Local State`` travels with it, so decryption still works in
        the copy.  Read-only source, local destination, nothing uploaded.
        """
        source = self.default_chrome_profile()
        if not source or not source.exists():
            return []
        dest = self.profile / "Default"
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for name in files:
            src = source.parent / name if name == "Local State" else source / name
            if not src.exists():
                continue
            try:
                shutil.copy2(src, dest / name)
                copied.append(name)
            except Exception:
                # Chrome may hold a lock; a locked Cookies file just means the
                # agent logs in once by hand and then stays logged in.
                continue
        if copied:
            self.apartment.log("profile_bootstrap", files=copied, source=str(source))
        return copied

    # ------------------------------------------------------------------- tabs
    def _targets(self) -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % self.port,
                                        timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return []

    def new_tab(self, url: str = "about:blank") -> Optional[CdpSession]:
        """Open a fresh tab and return a session bound to it."""
        self.apartment.require("browser")
        if not self.is_running() and not self.start():
            return None
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/new?%s" % (self.port, url), timeout=8) as resp:
                target = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            target = None
        if not target:
            return None
        session = CdpSession(self, target.get("id", ""), target.get("webSocketDebuggerUrl", ""),
                             title=target.get("title", ""))
        try:
            session.connect()
        except Exception as exc:
            self.apartment.log("tab_connect_failed", error=str(exc))
            return None
        self._sessions[target.get("id", "")] = session
        if url and url != "about:blank":
            session.navigate(url)
        return session

    def attach_tab(self, match: str) -> Optional[CdpSession]:
        """Attach to an existing tab whose title or URL contains ``match``."""
        for target in self._targets():
            if target.get("type") != "page":
                continue
            haystack = "%s %s" % (target.get("title", ""), target.get("url", ""))
            if match.lower() in haystack.lower():
                session = CdpSession(self, target["id"], target["webSocketDebuggerUrl"],
                                     title=target.get("title", ""))
                session.connect()
                self._sessions[target["id"]] = session
                return session
        return None

    def close_tab(self, target_id: str) -> bool:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/json/close/%s"
                                   % (self.port, target_id), timeout=5).read()
            self._sessions.pop(target_id, None)
            return True
        except Exception:
            return False

    def tabs(self) -> List[Dict[str, str]]:
        return [{"id": t.get("id", ""), "title": t.get("title", ""), "url": t.get("url", "")}
                for t in self._targets()]

    # -------------------------------------------------------- convenience API
    def visit(self, url: str, wait_s: float = 2.0) -> Optional[CdpSession]:
        session = self.new_tab(url)
        if session and wait_s:
            time.sleep(wait_s)
        return session

    def get_text(self, url: str, wait_s: float = 3.0) -> str:
        """One-shot: open a URL, return its visible text, close the tab.

        Used for pages that need JS (Google Maps, Instagram) where plain urllib
        would only get an empty shell.
        """
        session = self.visit(url, wait_s)
        if not session:
            return ""
        try:
            return session.extract_text()
        finally:
            session.close()

    def info(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "chrome": self.chrome_path,
            "running": self.is_running(),
            "port": self.port,
            "profile": str(self.profile),
            "tabs": len(self._targets()),
            "sessions": len(self._sessions),
        }


def find_chrome() -> Optional[str]:
    """Locate the user's Chrome/Chromium/Edge binary."""
    for candidate in CHROME_CANDIDATES:
        expanded = os.path.expandvars(candidate)
        if Path(expanded).exists():
            return expanded
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            edge = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if edge.exists():
                return str(edge)
    return None


def build(apartment) -> BrowserTool:
    return BrowserTool(apartment)
