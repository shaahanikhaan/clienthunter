"""Network tool: polite HTTP(S) fetch + DuckDuckGo search.

stdlib only (``urllib``).  Every call is guarded by:

*   the apartment's ``internet`` permission,
*   a per-host rate limiter (default 1 request / 2 seconds),
*   ``robots.txt`` (Disallow rules + Crawl-delay, fail-open with a log line),
*   a hard response-size cap so a hostile page cannot eat the 4 GB budget,
*   a global request budget per process (RAM + politeness).

If the network is unavailable the tool raises :class:`OfflineError`, which the
apartments translate into a graceful "skipped" instead of a crash.
"""

from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Dict, Iterable, List, Optional, Tuple

from ..base_apartment import PermissionDenied

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ClientHunterBot/1.1")

#: Maximum bytes we will buffer from any single response (RAM guard).
MAX_BYTES = 512 * 1024
#: Hard cap on requests per process, so a runaway loop cannot hammer the world.
REQUEST_BUDGET = 400


class OfflineError(RuntimeError):
    """Raised instead of raising urllib noise when the network is unreachable."""


class RateLimiter:
    """One token per ``interval`` seconds, per host."""

    def __init__(self, interval: float = 2.0):
        self.interval = float(interval)
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str) -> float:
        with self._lock:
            now = time.time()
            last = self._last.get(key, 0.0)
            delay = self.interval - (now - last)
            self._last[key] = max(now, last + self.interval)
        if delay > 0:
            time.sleep(delay)
        return max(delay, 0.0)


class RobotsPolicy:
    """Tiny ``robots.txt`` parser (User-agent groups, Allow/Disallow, Crawl-delay)."""

    def __init__(self, body: str):
        self.rules: List[Tuple[str, str]] = []   # (allow/disallow, path prefix)
        self.crawl_delay: Optional[float] = None
        self._parse(body or "")

    def _parse(self, body: str) -> None:
        applies = False
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                token = value.lower()
                applies = token == "*" or "clienthunter" in token or token.startswith("mozilla")
            elif applies and key in ("disallow", "allow"):
                if value:
                    self.rules.append((key, value))
            elif applies and key == "crawl-delay":
                try:
                    self.crawl_delay = float(value)
                except ValueError:
                    pass

    def allowed(self, path: str) -> bool:
        """Prefix matching, longest rule wins (Google's simplified behaviour)."""
        verdict, best = True, -1
        for kind, prefix in self.rules:
            if path.startswith(prefix) and len(prefix) > best:
                verdict, best = kind == "allow", len(prefix)
        return verdict


class WebTool:
    """Polite HTTP client bound to one apartment."""

    def __init__(self, apartment):
        self.apartment = apartment
        self.limiter = RateLimiter(
            float(apartment.config.get("limits", {}).get("request_interval_s", 2.0)))
        self.timeout = float(apartment.config.get("limits", {}).get("timeout_s", 12))
        self.max_bytes = int(apartment.config.get("limits", {}).get("max_bytes", MAX_BYTES))
        self.budget = int(apartment.config.get("limits", {}).get("request_budget", REQUEST_BUDGET))
        self._robots: Dict[str, RobotsPolicy] = {}
        self._used = 0
        self.stats = {"fetches": 0, "bytes": 0, "blocked_by_robots": 0, "failures": 0}
        self.respect_robots = bool(apartment.config.get("limits", {}).get("respect_robots", True))

    # ------------------------------------------------------------------ guards
    def _allow(self) -> None:
        if not self.apartment.can("internet"):
            raise PermissionDenied("apt_%s: internet permission denied"
                                   % self.apartment.NAME)
        if self._used >= self.budget:
            raise OfflineError("request budget exhausted (%d)" % self.budget)

    def _robots_for(self, host: str, scheme: str) -> Optional[RobotsPolicy]:
        if host in self._robots:
            return self._robots[host]
        policy = None
        try:
            raw = self._raw("%s://%s/robots.txt" % (scheme, host), respect_rate=False,
                            robots_check=False, max_bytes=64 * 1024)
            if raw is not None:
                policy = RobotsPolicy(raw)
        except Exception:
            policy = None
        self._robots[host] = policy
        return policy

    def allowed(self, url: str) -> bool:
        """Is this URL fetchable given permissions, robots.txt and budget?"""
        if not self.apartment.can("internet") or self._used >= self.budget:
            return False
        if not self.respect_robots:
            return True
        parts = urllib.parse.urlsplit(url)
        policy = self._robots_for(parts.netloc, parts.scheme or "https")
        if policy is None:
            return True
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return policy.allowed(path)

    # ------------------------------------------------------------------- fetch
    def _raw(self, url: str, respect_rate: bool = True, robots_check: bool = True,
             max_bytes: Optional[int] = None, method: str = "GET",
             data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None
             ) -> Optional[str]:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError("unsupported scheme: %s" % parts.scheme)
        if respect_rate:
            delay = None
            if self.respect_robots and robots_check:
                policy = self._robots_for(parts.netloc, parts.scheme)
                if policy and policy.crawl_delay:
                    delay = policy.crawl_delay
            self.limiter.wait(parts.netloc) if delay is None else time.sleep(delay)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        req.add_header("Accept-Language", "en-US,en;q=0.9,ur;q=0.8")
        req.add_header("Accept-Encoding", "gzip, deflate")
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        cap = max_bytes or self.max_bytes
        ctx = ssl.create_default_context()
        ctx.check_hostname = False          # many Pakistani SME sites have bad certs
        ctx.verify_mode = ssl.CERT_NONE     # we audit certs explicitly in audit.py
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read(cap + 1)
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if "gzip" in encoding:
                    raw = gzip.decompress(raw)
                elif "deflate" in encoding:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                if len(raw) > cap:
                    raw = raw[:cap]
                charset = resp.headers.get_content_charset() or "utf-8"
                self._used += 1
                self.stats["fetches"] += 1
                self.stats["bytes"] += len(raw)
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            self.stats["failures"] += 1
            if exc.code in (403, 429):
                raise OfflineError("HTTP %d (rate limited / blocked): %s" % (exc.code, url))
            return None
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            self._used += 1
            self.stats["failures"] += 1
            raise OfflineError("cannot fetch %s: %s" % (url, exc))

    def get(self, url: str, max_bytes: Optional[int] = None) -> Optional[str]:
        """Fetch a URL as text.  Returns ``None`` on 4xx/5xx, raises when offline."""
        self._allow()
        if not self.allowed(url):
            self.stats["blocked_by_robots"] += 1
            self.apartment.log("robots_blocked", url=url)
            return None
        return self._raw(url, max_bytes=max_bytes)

    def head(self, url: str) -> Optional[Dict[str, str]]:
        """Cheap existence check.  Many shared hosts reject HEAD, so we retry GET."""
        self._allow()
        try:
            return self._raw(url, method="HEAD", max_bytes=1) is not None and {"ok": "1"} or None
        except Exception:
            try:
                return {"ok": "1"} if self._raw(url, max_bytes=2048) else None
            except OfflineError:
                return None

    def exists(self, url: str) -> bool:
        try:
            return self._raw(url, max_bytes=1024) is not None
        except OfflineError:
            return False

    # ------------------------------------------------------------------ search
    def search(self, query: str, count: int = 10) -> List[Dict[str, str]]:
        """DuckDuckGo HTML search (no API key).  Returns ``[{title,url,snippet}]``."""
        self._allow()
        results: List[Dict[str, str]] = []
        for endpoint in ("https://html.duckduckgo.com/html/",
                         "https://lite.duckduckgo.com/lite/"):
            body = self._post(endpoint, {"q": query, "kl": "pk-en"})
            if not body:
                continue
            results = self._parse_results(body)
            if results:
                break
        return results[:count]

    def _post(self, url: str, fields: Dict[str, str]) -> Optional[str]:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        try:
            return self._raw(url, data=data,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        except OfflineError:
            return None

    @staticmethod
    def _parse_results(html: str) -> List[Dict[str, str]]:
        """Regex extraction - deliberately dependency-free."""
        out: List[Dict[str, str]] = []
        link_re = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
        snippet_re = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</a>', re.S | re.I)
        snippets = [_strip_tags(s) for s in snippet_re.findall(html)]
        for idx, (href, title) in enumerate(link_re.findall(html)):
            url = _decode_ddg(href)
            out.append({
                "title": _strip_tags(title),
                "url": url,
                "snippet": snippets[idx] if idx < len(snippets) else "",
            })
        if out:
            return out
        # lite.duckduckgo.com markup
        for href, title in re.findall(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                                      html, re.S | re.I):
            out.append({"title": _strip_tags(title), "url": _decode_ddg(href), "snippet": ""})
        return out

    # ------------------------------------------------------------------ extras
    def report(self) -> Dict[str, object]:
        return dict(self.stats, budget_left=self.budget - self._used)


def _decode_ddg(href: str) -> str:
    """DDG wraps results in ``//duckduckgo.com/l/?uddg=<urlencoded>``."""
    if "uddg=" in href:
        href = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg", [href])[0]
    return urllib.parse.unquote(href)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    for entity, char in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
                         ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip()


def build(apartment) -> WebTool:
    return WebTool(apartment)
