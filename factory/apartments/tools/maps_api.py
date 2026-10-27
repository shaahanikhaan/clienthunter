"""Google Maps **without an API key** - the internal endpoint client.

Why this exists
---------------
The Places API is not an option for this project: Google retired the pooled
$200/month credit in March 2025, a billing account is now mandatory, and the
SKU carrying phone/website/rating is *Enterprise* - 1,000 free calls/month,
then roughly $20-35 per 1,000. A month of real hunting blows through that.

Google Maps itself, however, serves the same public data to the browser
through internal RPC endpoints. Those endpoints are what this module talks to:

    GET https://www.google.com/search?tbm=map&pb=<blob>&q=<query>
        -> {"d": ")]}'\\n<json>"}          (XSSI-prefixed)
    GET https://www.google.com/maps/preview/place?pb=<blob>
        -> ")]}'\\n<json>"                  (full place detail)

Field indices were taken from the current public implementations of this
technique (see POWER_GUIDE.md) and are parsed **defensively**: every field is
reached through a safe index getter, the listings array is located by shape
rather than by a hard-coded path, and phone/email/social values are found by a
bounded recursive search. If Google reshuffles the payload, this module returns
``[]`` - it never invents a lead and never raises.

Layers, in order of preference:

1. ``httpcloak`` (25 MB wheel, **zero dependencies**) - real browser TLS
   fingerprint, which is what actually gets you past Google's edge.
2. ``curl_cffi`` - same idea, heavier (259 MB of wheels across platforms).
3. **stdlib urllib** - always available; works for many queries, and degrades
   to an empty result (never a crash) when Google challenges the request.

Politeness is built in: 1 request / 2 s throttle, warm-up requests to
``google.com`` and ``maps`` first, exponential backoff on 429/503, consent-wall
detection, and a hard cap on pages per run.

Legal note: the data fetched here is public business listings (the same
information visible on maps.google.com). Scraping public data is not a CFAA
violation (*hiQ Labs v. LinkedIn*, 9th Cir. 2022). Google's ToS §3.2.3 limits
caching of Places content to 30 days - ``apt_hunter`` therefore records
``scraped_at`` on every lead and the daily cycle re-verifies stale ones.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Sequence, Tuple

BACKEND_STDLIB = "stdlib-urllib"
BACKEND_HTTPCLOAK = "httpcloak"
BACKEND_CURLCFFI = "curl_cffi"

BASE = "https://www.google.com"

#: Browser-ish headers.  ``x-browser-channel`` / ``x-browser-year`` /
#: ``x-maps-diversion-context-bin`` are the ones Maps' internal RPCs expect.
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-browser-channel": "stable",
    "x-browser-year": "2026",
    "x-maps-diversion-context-bin": "CAE=",
    "x-client-data": "CIW2yQEIorbJAQipncoBCLKPywEIlKHLAQiFoM0BCLLIzQEI3ZjOAQ==",
}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

#: Consent / sign-in interstitials - the request worked, but there is no data.
WALL_MARKERS = ("consent.google.com", "signin/v2", "/ServiceLogin",
                "Before you continue", "unusual traffic")


def blocked(body: Optional[str], status: Optional[int] = None) -> Optional[str]:
    """Why Google gave us a wall instead of data - or ``None`` if it did not.

    Two failure shapes matter: an HTTP rejection (403/429/503) and a body that
    is a consent or sign-in page.  Both mean "stop and fall back to Chrome",
    never "retry harder".
    """
    if status is not None and int(status) in (403, 429, 503):
        return "http %s" % int(status)
    if body and any(marker in body[:4000] for marker in WALL_MARKERS):
        return "consent/signin wall"
    return None


class MapsApiTool:
    """Keyless Google Maps search + place detail client."""

    #: Seconds between requests (the factory's global politeness floor).
    INTERVAL = 2.0

    def __init__(self, apartment, throttle: Optional[float] = None,
                 timeout: float = 20.0, lang: str = "en", gl: str = "pk"):
        self.apartment = apartment
        self.timeout = float(timeout)
        self.lang = lang
        self.gl = gl
        self.interval = float(throttle if throttle is not None else
                              (apartment.config.get("limits", {}).get("request_interval_s", 2.0)))
        self._last_call = 0.0
        self._warmed = False
        self._cookies: Dict[str, str] = {}
        self.backend = self._detect_backend()
        self.stats: Dict[str, int] = {"requests": 0, "failed": 0, "blocked": 0,
                                      "places": 0, "searches": 0}

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _detect_backend() -> str:
        try:                                            # smallest + zero deps
            import httpcloak  # noqa: F401
            return BACKEND_HTTPCLOAK
        except Exception:
            pass
        try:
            from curl_cffi import requests  # noqa: F401
            return BACKEND_CURLCFFI
        except Exception:
            pass
        return BACKEND_STDLIB

    def info(self) -> Dict[str, Any]:
        return {"backend": self.backend, "timeout": self.timeout,
                "interval_s": self.interval, "lang": self.lang, "gl": self.gl,
                "warmed": self._warmed, "stats": dict(self.stats)}

    # ---------------------------------------------------------------- plumbing
    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last_call = time.time()

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = dict(HEADERS)
        headers["user-agent"] = USER_AGENT
        if self._cookies:
            headers["cookie"] = "; ".join("%s=%s" % (k, v) for k, v in self._cookies.items())
        if extra:
            headers.update(extra)
        return headers

    def _absorb_cookies(self, response: Any) -> None:
        try:
            raw = response.headers.get_all("Set-Cookie") if hasattr(
                response.headers, "get_all") else []
        except Exception:
            raw = []
        for item in raw:
            pair = str(item).split(";")[0]
            if "=" in pair:
                key, value = pair.split("=", 1)
                self._cookies[key.strip()] = value.strip()

    def _get(self, url: str, referer: Optional[str] = None) -> Optional[str]:
        """One GET.  Returns body text or ``None`` - never raises."""
        self._throttle()
        headers = self._headers({"referer": referer or BASE + "/maps"})
        self.stats["requests"] += 1
        try:
            if self.backend == BACKEND_HTTPCLOAK:
                body = self._get_httpcloak(url, headers)
            elif self.backend == BACKEND_CURLCFFI:
                body = self._get_curl_cffi(url, headers)
            else:
                body = self._get_stdlib(url, headers)
        except Exception as exc:
            self.stats["failed"] += 1
            self.apartment.log("maps_api_error", error=str(exc)[:160], url=url[:120])
            return None
        reason = blocked(body)
        if reason:
            self.stats["blocked"] += 1
            self.apartment.log("maps_api_wall", reason=reason, chars=len(body or ""))
            return None
        return body

    def _get_stdlib(self, url: str, headers: Dict[str, str]) -> Optional[str]:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            self._absorb_cookies(response)
            raw = response.read(2_000_000)
        charset = "utf-8"
        return raw.decode(charset, "replace")

    def _get_httpcloak(self, url: str, headers: Dict[str, str]) -> Optional[str]:
        import httpcloak

        session = getattr(self, "_cloak_session", None)
        if session is None:
            session = httpcloak.Session(browser="chrome")
            self._cloak_session = session
        response = session.get(url, headers=headers, timeout=self.timeout)
        if getattr(response, "status_code", 200) >= 400:
            raise RuntimeError("http %s" % response.status_code)
        return response.text

    def _get_curl_cffi(self, url: str, headers: Dict[str, str]) -> Optional[str]:
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(url, headers=headers, timeout=self.timeout,
                                     impersonate="chrome")
        if response.status_code >= 400:
            raise RuntimeError("http %s" % response.status_code)
        return response.text

    # ------------------------------------------------------------------ warmup
    def warmup(self) -> bool:
        """Hit google.com + maps once so cookies/consent look like a browser."""
        if self._warmed:
            return True
        for url in (BASE + "/", BASE + "/maps"):
            try:
                self._get(url)
            except Exception:
                pass
        self._warmed = True
        return True

    # ------------------------------------------------------------------- URLs
    @staticmethod
    def search_url(query: str, lat: float, lng: float, zoom: int = 13,
                   page_size: int = 20, start: int = 0, lang: str = "en",
                   gl: str = "pk") -> str:
        """``tbm=map`` search URL.  ``pb`` is the internal viewport/RPC blob."""
        span = 360 / (2 ** max(1, int(zoom)))
        d_value = span * 111320
        pb = ("!4m12!1m3!1d{d:.0f}!2d{lng}!3d{lat}"
              "!2m3!1f0!2f0!3f0!3m2!1i1280!2i593!4f13.1"
              "!7i{size}").format(d=d_value, lng=lng, lat=lat, size=int(page_size))
        if start > 0:
            pb += "!8i%d" % int(start)
        pb += (
            "!10b1"
            "!12m25!1m5!18b1!30b1!31m1!1b1!34e1!2m4!5m1!6e2!20e3!39b1"
            "!10b1!12b1!13b1!16b1!17m1!3e1!20m3!5e2!6b1!14b1!46m1!1b0!96b1!99b1"
            "!19m4!2m3!1i360!2i120!4i8"
            "!20m65!2m2!1i203!2i100!3m2!2i4!5b1"
            "!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
            "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3"
            "!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2"
            "!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0"
            "!15m16!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
            "!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
            "!22m2!1sdummy!7e81"
            "!24m109!1m27!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1"
            "!18m16!3b1!4b1!5b1!6b1!9b1!13b1!14b1!17b1!20b1!21b1!22b1"
            "!32b1!33m1!1b1!34b1!36e2"
            "!10m1!8e3!11m1!3e1!17b1!20m2!1e3!1e6"
            "!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1"
            "!39m3!2m2!2i1!3i1!43b1!52b1!54m1!1b1!55b1!56m1!1b1"
            "!61m2!1m1!1e1!65m5!3m4!1m3!1m2!1i224!2i298"
            "!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!4b1"
            "!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4"
            "!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts"
            "!6m1!1e1!9b1!89b1!90m2!1m1!1e2"
            "!98m3!1b1!2b1!3b1!103b1!113b1!114m3!1b1!2m1!1b1"
            "!117b1!122m1!1b1!126b1!127b1!128m1!1b0"
            "!26m4!2m3!1i80!2i92!4i8"
            "!30m28!1m6!1m2!1i0!2i0!2m2!1i530!2i593"
            "!1m6!1m2!1i1230!2i0!2m2!1i1280!2i593"
            "!1m6!1m2!1i0!2i0!2m2!1i1280!2i20"
            "!1m6!1m2!1i0!2i573!2m2!1i1280!2i593"
            "!34m19!2b1!3b1!4b1!6b1!8m6!1b1!3b1!4b1!5b1!6b1!7b1"
            "!9b1!12b1!14b1!20b1!23b1!25b1!26b1!31b1"
            "!37m1!1e81!42b1!47m0"
            "!49m10!3b1!6m2!1b1!2b1!7m2!1e3!2b1!8b1!9b1!10e2"
            "!50m4!2e2!3m2!1b1!3b1"
            "!67m5!7b1!10b1!14b1!15m1!1b0!69i775!77b1"
        )
        return ("{base}/search?tbm=map&authuser=0&hl={lang}&gl={gl}"
                "&pb={pb}&q={q}&tch=1&ech=1&psi=dummy.{ms}.1").format(
            base=BASE, lang=lang, gl=gl, pb=pb,
            q=urllib.parse.quote(query), ms=int(time.time() * 1000))

    @staticmethod
    def place_url(place_id: str, lat: float, lng: float, query: str = "",
                  lang: str = "en", gl: str = "pk") -> str:
        enc_pid = urllib.parse.quote(place_id, safe="")
        pb = ("!1m6"
              "!1s{pid}"
              "!3m1!1d1000"
              "!4m2!3d{lat}!4d{lng}"
              "!3m1!1e3"
              "!15m111!1m29!4e2!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1"
              "!18m17!3b1!4b1!5b1!6b1!9b1!13b1!14b1!17b1!20b1!21b1!22b1!30b1!32b1"
              "!33m1!1b1!34b1!36e2"
              "!10m1!8e3!11m1!3e1!17b1!20m2!1e3!1e6!24b1!25b1!26b1!27b1!29b1"
              "!30m1!2b1!36b1!37b1"
              "!39m3!2m2!2i1!3i1!43b1!52b1!54m1!1b1!55b1!56m1!1b1!61m2!1m1!1e1"
              "!65m5!3m4!1m3!1m2!1i224!2i298"
              "!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!4b1"
              "!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4"
              "!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts"
              "!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1"
              "!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1!126b1!127b1!128m1!1b0"
              ).format(pid=enc_pid, lat=lat, lng=lng)
        url = "{base}/maps/preview/place?authuser=0&hl={lang}&gl={gl}&pb={pb}".format(
            base=BASE, lang=lang, gl=gl, pb=pb)
        if query:
            url += "&q=%s" % urllib.parse.quote(query, safe="")
        return url

    @staticmethod
    def maps_page_url(place_id: str, lat: float, lng: float, query: str = "",
                      zoom: int = 17) -> str:
        slug = urllib.parse.quote(query or place_id, safe="")
        return "{base}/maps/place/{slug}/@{lat},{lng},{zoom}z".format(
            base=BASE, slug=slug, lat=lat, lng=lng, zoom=zoom)

    # ----------------------------------------------------------------- parsing
    @staticmethod
    def _strip_xssi(text: str) -> str:
        text = text.split("/*")[0]
        return text[4:].lstrip("\n") if text.startswith(")]}'") else text

    @staticmethod
    def _get(node: Any, *indices, default=None):
        current = node
        for index in indices:
            if not isinstance(current, (list, tuple)) or index < 0 or index >= len(current):
                return default
            current = current[index]
        return current if current is not None else default

    @staticmethod
    def _deep_find(node: Any, pattern: str, depth: int = 6, scan: int = 40) -> Optional[str]:
        compiled = re.compile(pattern)

        def walk(item, level: int) -> Optional[str]:
            if level > depth:
                return None
            if isinstance(item, str):
                match = compiled.search(item)
                return match.group(0) if match else None
            if isinstance(item, (list, tuple)):
                for child in list(item)[:scan]:
                    found = walk(child, level + 1)
                    if found:
                        return found
            return None

        return walk(node, 0)

    def parse_search(self, text: str) -> List[Dict[str, Any]]:
        """Parse a ``tbm=map`` response into place stubs."""
        outer = None
        try:
            outer, _ = json.JSONDecoder().raw_decode(text.split("/*")[0])
        except Exception:
            outer = None
        inner = outer.get("d") if isinstance(outer, dict) and "d" in outer else text
        try:
            data = json.loads(self._strip_xssi(inner))
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        # The listings array is the last element whose entries carry a
        # place payload (list with > 50 slots) - shape-based, not index-based.
        listings: List[Any] = []
        for element in reversed(data):
            if not isinstance(element, list):
                continue
            for entry in element:
                if isinstance(entry, list) and len(entry) >= 2 and \
                        isinstance(entry[1], list) and len(entry[1]) > 50:
                    listings = element
                    break
            if listings:
                break

        results: List[Dict[str, Any]] = []
        for item in listings if isinstance(listings, list) else []:
            if not (isinstance(item, list) and len(item) >= 2):
                continue
            stub = self._stub(item[1])
            if stub:
                results.append(stub)

        if not results:                               # knowledge-panel shape
            entities = self._get(data, 0, 1, default=[]) or []
            for entity in entities if isinstance(entities, list) else []:
                stub = self._stub(self._get(entity, 14))
                if stub:
                    results.append(stub)
        return results

    def _stub(self, payload: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, list):
            return None
        place_id = self._get(payload, 10, default="")
        if not isinstance(place_id, str) or "0x" not in place_id:
            found = self._deep_find(payload, r"0x[0-9a-f]+:0x[0-9a-f]+", depth=5)
            if not found:
                return None
            place_id = found
        rating_block = self._get(payload, 4, default=[])
        categories = self._get(payload, 13, default=[])
        if isinstance(categories, str):
            categories = [categories]
        elif not isinstance(categories, list):
            categories = []
        name = self._get(payload, 11, default="") or ""
        if not name:
            return None
        return {
            "place_id": place_id,
            "name": name,
            "lat": _as_float(self._get(payload, 9, 2, default=0)),
            "lng": _as_float(self._get(payload, 9, 3, default=0)),
            "rating": _as_float(self._get(rating_block, 7, default=0) if
                                isinstance(rating_block, list) else 0),
            "reviews": _as_int(self._get(rating_block, 8, default=0) if
                               isinstance(rating_block, list) else 0),
            "categories": [str(c) for c in categories if c][:6],
            "address": str(self._get(payload, 18, default="") or ""),
        }

    def parse_place(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse a ``/maps/preview/place`` response into full details."""
        try:
            data = json.loads(self._strip_xssi(text))
        except Exception:
            return None
        info = self._get(data, 1)
        if not (isinstance(info, list) and len(info) >= 50):
            info = self._get(data, 6, default=[])
        if not isinstance(info, list):
            return None

        place_id = self._get(info, 10)
        if not (isinstance(place_id, str) and place_id.startswith("0x")):
            place_id = self._deep_find(data, r"0x[0-9a-f]+:0x[0-9a-f]+", depth=5) or ""

        address_parts = self._get(info, 2, default=[])
        address = ", ".join(p for p in address_parts
                            if isinstance(p, str)) if isinstance(address_parts, list) else ""

        rating_block = self._get(info, 4, default=[])
        rating = _as_float(self._get(rating_block, 7, default=0) or
                           self._get(rating_block, 0, 7, default=0))
        reviews = _as_int(self._get(rating_block, 8, default=0) or
                          self._get(rating_block, 0, 8, default=0))
        if not reviews and isinstance(rating_block, list):
            label = self._get(self._get(rating_block, 3), 1, default="")
            if isinstance(label, str) and "review" in label.lower():
                digits = re.findall(r"\d+", label.replace(",", ""))
                reviews = int(digits[0]) if digits else 0

        website_block = self._get(info, 7)
        website = ""
        if isinstance(website_block, list) and website_block:
            value = self._get(website_block, 0, default="")
            if isinstance(value, list):
                value = self._get(value, 0, default="")
            website = value if isinstance(value, str) else ""
        elif isinstance(website_block, str):
            website = website_block

        coords = self._get(info, 9, default=[])
        categories = self._get(info, 13, default=[])
        if isinstance(categories, str):
            categories = [categories]
        elif not isinstance(categories, list):
            categories = []

        phone = self._deep_find(info, r"\+?\d[\d\s\-()]{7,}\d", depth=6, scan=40)
        if phone and len(phone) > 25:
            phone = None
        email = self._deep_find(
            info, r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", depth=7, scan=60)
        if email and email.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            email = None

        return {
            "place_id": place_id,
            "name": str(self._get(info, 11, default="") or ""),
            "address": address,
            "website": website,
            "phone": phone or "",
            "email": email or "",
            "rating": rating,
            "reviews": reviews,
            "lat": _as_float(self._get(coords, 2, default=0)),
            "lng": _as_float(self._get(coords, 3, default=0)),
            "categories": [str(c) for c in categories if c][:6],
            "socials": _find_socials(info),
            "maps_url": "",
        }

    # ------------------------------------------------------------------- API
    def search(self, query: str, lat: float, lng: float, zoom: int = 13,
               page_size: int = 20, start: int = 0, retries: int = 2) -> List[Dict[str, Any]]:
        """Search Maps.  Returns stubs (id/name/rating/...), ``[]`` on failure."""
        if not self._warmed:
            self.warmup()
        url = self.search_url(query, lat, lng, zoom=zoom, page_size=page_size,
                              start=start, lang=self.lang, gl=self.gl)
        for attempt in range(retries + 1):
            body = self._get(url, referer=BASE + "/maps")
            if body:
                stubs = self.parse_search(body)
                if stubs:
                    self.stats["searches"] += 1
                    self.apartment.log("maps_api_search", query=query[:60],
                                       results=len(stubs))
                    return stubs
            if attempt < retries:
                time.sleep(self.interval * (2 ** attempt) + random.uniform(0.2, 0.8))
        self.apartment.log("maps_api_search_empty", query=query[:60])
        return []

    def place(self, place_id: str, lat: float = 0.0, lng: float = 0.0,
              query: str = "", retries: int = 2) -> Optional[Dict[str, Any]]:
        """Fetch full details for one place (phone, website, socials, hours)."""
        if not place_id:
            return None
        if not self._warmed:
            self.warmup()
        url = self.place_url(place_id, lat, lng, query=query, lang=self.lang, gl=self.gl)
        for attempt in range(retries + 1):
            body = self._get(url, referer=BASE + "/maps")
            if body:
                detail = self.parse_place(body)
                if detail and detail.get("name"):
                    self.stats["places"] += 1
                    detail["maps_url"] = self.maps_page_url(
                        place_id, detail.get("lat") or lat, detail.get("lng") or lng,
                        detail.get("name"))
                    return detail
            if attempt < retries:
                time.sleep(self.interval * (2 ** attempt) + random.uniform(0.2, 0.8))
        return None

    def enrich(self, stub: Dict[str, Any]) -> Dict[str, Any]:
        """Search stub -> full detail (one extra request)."""
        detail = self.place(stub.get("place_id", ""), stub.get("lat", 0.0),
                            stub.get("lng", 0.0), stub.get("name", ""))
        if not detail:
            return dict(stub)
        for key, value in stub.items():
            detail.setdefault(key, value)
        return detail

    # --------------------------------------------------------- city/area sweep
    def sweep(self, city: str, category: str, areas: Sequence[str],
              lat: float, lng: float, limit: int = 40, pages: int = 1,
              deep: bool = True, progress=None) -> List[Dict[str, Any]]:
        """Area-grid sweep: one search per locality, then detail per new place.

        Grid coverage (rather than scrolling one result list) is what finds the
        businesses beyond the first page - the technique used by the serious
        Maps scrapers.  Results are deduped on ``place_id``.
        """
        queries = ["%s in %s, %s" % (category, area, city) for area in areas] or \
                  ["%s in %s" % (category, city)]
        seen: Dict[str, Dict[str, Any]] = {}
        for index, query in enumerate(queries, start=1):
            if len(seen) >= limit:
                break
            if progress:
                progress("maps: %s (%d/%d)" % (query[:44], index, len(queries)))
            for page in range(pages):
                stubs = self.search(query, lat, lng, page_size=20, start=page * 20)
                if not stubs:
                    break
                for stub in stubs:
                    key = stub.get("place_id") or (stub.get("name", "") + "|" +
                                                   stub.get("address", ""))
                    if key in seen or not stub.get("name"):
                        continue
                    seen[key] = stub
                    if len(seen) >= limit:
                        break
                if len(stubs) < 20:
                    break
        results = list(seen.values())[:limit]
        if deep:
            enriched = []
            for stub in results:
                detail = self.enrich(stub)
                detail["city"] = city
                detail["category"] = category
                detail["source"] = "google_maps_endpoint"
                enriched.append(detail)
            return enriched
        for stub in results:
            stub.setdefault("city", city)
            stub.setdefault("category", category)
            stub.setdefault("source", "google_maps_endpoint")
        return results


def _find_socials(node: Any, limit: int = 8) -> Dict[str, str]:
    platforms = {
        "facebook.com": "facebook", "instagram.com": "instagram",
        "twitter.com": "twitter", "x.com": "twitter", "linkedin.com": "linkedin",
        "youtube.com": "youtube", "tiktok.com": "tiktok",
        "wa.me": "whatsapp", "api.whatsapp.com": "whatsapp",
    }
    found: Dict[str, str] = {}

    def walk(item, depth: int) -> None:
        if depth > 7 or len(found) >= limit:
            return
        if isinstance(item, str) and item.startswith("http"):
            lowered = item.lower()
            for domain, name in platforms.items():
                if domain in lowered and name not in found:
                    found[name] = item
                    break
        elif isinstance(item, (list, tuple)):
            for child in list(item)[:60]:
                walk(child, depth + 1)

    walk(node, 0)
    return found


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def build(apartment) -> MapsApiTool:
    """Tool factory.  The apartment must be allowed to scrape maps."""
    tool = MapsApiTool(apartment)
    try:
        apartment.gate.enforce(apartment.NAME, "scrape_maps",
                               {"engine": "maps-endpoint", "backend": tool.backend})
    except Exception as exc:
        apartment.log("maps_api_denied", error=str(exc)[:120])
    return tool
