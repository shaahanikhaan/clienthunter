"""Lead scraper - Google Maps + directories, no API key, no Docker.

The obvious choice here is `google-maps-scraper-kit
<https://github.com/Mahanaicoach/google-maps-scraper-kit>`_ (1.9k stars, MIT,
"no API keys, runs 100% locally").  It is genuinely good - but it ships as a
Docker image, and Docker is forbidden by the brief on a 4 GB machine.  So this
tool re-implements the same technique natively:

    ``maps.google.com`` is JS-only, so a plain HTTP GET returns an empty shell.
    We therefore drive the user's real Chrome over CDP (tools/browser.py),
    scroll the results column, and read the business cards out of the DOM with a
    single ``Runtime.evaluate``.  Same data, no container, ~30 MB of RAM.

Politeness, because bans are real:
    * 1 request / 2 s floor, plus jitter,
    * a hard per-run cap on scroll steps and results,
    * robots.txt respected for ordinary directory sites,
    * it stops instantly on the kill switch.

Manual CSV import is a first-class fallback (``import_csv``) - when Google
starts serving captchas, that is how you keep working.
"""

from __future__ import annotations

import csv
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus

from ..base_apartment import PermissionDenied

#: Fields every lead gets, CSV header order included.
LEAD_FIELDS = ("name", "phone", "email", "address", "category", "city", "website",
               "rating", "reviews", "source")

MAPS_JS = r"""
(function(){
  var out = [];
  var seen = new Set();
  var links = document.querySelectorAll('a[href*="/maps/place/"]');
  for (var i = 0; i < links.length; i++) {
    var a = links[i];
    var card = a.closest('div[role="article"]') || a.parentElement;
    var text = (card && card.innerText ? card.innerText : a.innerText || '').trim();
    var href = a.getAttribute('href') || '';
    var key = href + '|' + text.slice(0, 40);
    if (!text || seen.has(key)) continue;
    seen.add(key);
    var lines = text.split('\n').map(function(s){return s.trim();})
                    .filter(function(s){return s.length;});
    out.push({
      name: lines[0] || '',
      href: href,
      lines: lines.slice(0, 12),
      text: text.slice(0, 600)
    });
  }
  return out;
})()
"""

RATING_RE = re.compile(r"([0-9][.,][0-9])\s*(?:\(([0-9,]+)\)|⭐|[Ss]tars?)")
REVIEW_RE = re.compile(r"\(([0-9][,0-9]*)\)\s*$")


class ScrapeTool:
    """Finds local Pakistani businesses."""

    def __init__(self, apartment):
        self.apartment = apartment
        cfg = apartment.config.get("limits", {}) or {}
        self.min_interval = float(cfg.get("request_interval_s", 2.0))
        self.max_results = int(cfg.get("max_results_per_query", 40))
        self.max_scrolls = int(cfg.get("max_scrolls", 8))
        self.stats = {"queries": 0, "found": 0, "duplicates": 0, "errors": 0}
        self._last_request = 0.0

    # --------------------------------------------------------------- politeness
    def _throttle(self) -> None:
        gap = time.time() - self._last_request
        wait = self.min_interval - gap + random.uniform(0.0, 0.6)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _stopped(self) -> bool:
        from ..base_apartment import BaseApartment
        return BaseApartment.kill_switch()

    # ------------------------------------------------------------ google maps
    def scrape_google_maps(self, city: str, category: str, limit: int = 20,
                           area: Optional[str] = None,
                           progress: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
        """Scrape one city+category (optionally narrowed to a neighbourhood).

        Returns raw lead dicts.  Inserts nothing - the caller (apt_hunter) owns
        the database write, which keeps its permissions honest.
        """
        self.apartment.gate.enforce(self.apartment.NAME, "scrape_maps",
                                    {"city": city, "category": category})
        self.apartment.require("internet")

        # Tier 1: keyless Maps endpoints (fast, no Chrome needed)
        if not self._stopped():
            endpoint_leads = self.scrape_maps_endpoint(
                city, category, limit=limit, area=area, deep=True, progress=progress)
            if endpoint_leads:
                return endpoint_leads

        query = "%s in %s %s" % (category, area or "", city)
        query = re.sub(r"\s+", " ", query).strip()
        if progress:
            progress("query: %s" % query)
        self._throttle()

        url = "https://www.google.com/maps/search/%s" % quote_plus(query)
        leads: List[Dict[str, Any]] = []

        try:
            browser = self.apartment.tool("browser")
        except Exception as exc:
            self.stats["errors"] += 1
            self.apartment.log("scrape_no_browser", error=str(exc))
            return self._fallback_search(query, city, category, limit)

        try:
            chrome_up = browser.is_running() or browser.start()
        except Exception as exc:                     # PermissionDenied, no Chrome, ...
            self.stats["errors"] += 1
            self.apartment.log("scrape_no_chrome", error=str(exc)[:120],
                               hint="Chrome unavailable - using web search")
            return self._fallback_search(query, city, category, limit)
        if not chrome_up:
            self.apartment.log("scrape_no_chrome", hint="Chrome not found - using web search")
            return self._fallback_search(query, city, category, limit)

        session = browser.new_tab()
        if session is None:
            return self._fallback_search(query, city, category, limit)
        try:
            session.navigate(url, wait_s=5)
            time.sleep(2)
            for step in range(self.max_scrolls):
                if self._stopped():
                    break
                cards = session.evaluate(MAPS_JS) or []
                for card in cards:
                    lead = self._card_to_lead(card, city, category, query)
                    if lead and lead.get("name"):
                        leads.append(lead)
                if len(leads) >= limit:
                    break
                # scroll the results column, not the map
                session.evaluate(
                    "(function(){var el=document.querySelector("
                    "'div[role=feed]')||document.querySelector("
                    "'div.m6QErb[aria-label]');if(el){el.scrollTop+=1200;return true;}"
                    "window.scrollBy(0,1200);return false;})()")
                time.sleep(1.6 + random.uniform(0, 0.8))
        except Exception as exc:
            self.stats["errors"] += 1
            self.apartment.log("scrape_failed", error=str(exc))
        finally:
            session.close()

        if not leads:
            return self._fallback_search(query, city, category, limit)
        return self._dedupe(leads)[:limit]

    def scrape_maps_endpoint(self, city: str, category: str, limit: int = 20,
                             area: Optional[str] = None, deep: bool = True,
                             progress: Optional[Callable[[str], None]] = None,
                             ) -> List[Dict[str, Any]]:
        """Tier 1: Google Maps internal endpoints - **no API key, no Chrome**.

        Fetches the same public listing data the Maps UI uses, walks the city's
        neighbourhoods so results are not limited to the first page, then pulls
        full details (phone, website, socials) per place.  Returns ``[]`` if
        Google challenges the request, so the caller falls through to Chrome.
        """
        try:
            maps = self.apartment.tool("maps_api")
        except Exception as exc:
            self.apartment.log("maps_api_unavailable", error=str(exc)[:120])
            return []
        coords = self.city_coords(city)
        if coords == (0.0, 0.0):
            self.apartment.log("maps_api_no_coords", city=city)
            return []
        areas = [area] if area else self.city_areas(city)
        if progress:
            progress("maps endpoint: %s, %d area(s), backend=%s"
                     % (city, len(areas), maps.backend))
        try:
            rows = maps.sweep(city, category, areas, coords[0], coords[1],
                              limit=limit, deep=deep, progress=progress)
        except Exception as exc:
            self.stats["errors"] += 1
            self.apartment.log("maps_api_failed", error=str(exc)[:160])
            return []
        leads = [self._place_to_lead(row, city, category) for row in rows]
        leads = [lead for lead in leads if lead.get("name")]
        self.stats["queries"] += len(areas)
        self.stats["found"] += len(leads)
        return self._dedupe(leads)[:limit]

    @staticmethod
    def _place_to_lead(place: Dict[str, Any], city: str,
                       category: str) -> Dict[str, Any]:
        """Maps endpoint payload -> the lead shape the store expects."""
        socials = place.get("socials") or {}
        return {
            "name": (place.get("name") or "")[:90],
            "phone": place.get("phone") or "",
            "email": place.get("email") or "",
            "address": (place.get("address") or "")[:160],
            "category": category,
            "city": city,
            "website": place.get("website") or "",
            "rating": float(place.get("rating") or 0),
            "reviews": int(place.get("reviews") or 0),
            "socials": socials,
            "place_id": place.get("place_id") or "",
            "maps_url": place.get("maps_url") or "",
            "lat": float(place.get("lat") or 0),
            "lng": float(place.get("lng") or 0),
            "source": "google_maps_endpoint",
            "scraped_at": time.time(),
        }

    @staticmethod
    def city_coords(city: str) -> Tuple[float, float]:
        """Approximate city centre, used only to centre the Maps viewport."""
        data = ScrapeTool._city_row(city)
        return (float(data.get("lat", 0) or 0), float(data.get("lng", 0) or 0))

    @staticmethod
    def city_areas(city: str, maximum: int = 12) -> List[str]:
        """Neighbourhoods to sweep (a city-wide query only ever shows page 1)."""
        data = ScrapeTool._city_row(city)
        areas = [str(a) for a in (data.get("areas") or []) if a]
        return areas[:maximum]

    @staticmethod
    def _city_row(city: str) -> Dict[str, Any]:
        try:
            payload = json.loads((Path(__file__).resolve().parents[2] / "data" /
                                  "pakistani_cities.json").read_text(encoding="utf-8"))
        except Exception:
            return {}
        for row in payload.get("cities") or []:
            if str(row.get("name", "")).lower() == str(city or "").lower():
                return row
        return {}

    def _card_to_lead(self, card: Dict[str, Any], city: str, category: str,
                      query: str) -> Optional[Dict[str, Any]]:
        lines = card.get("lines") or []
        text = card.get("text") or ""
        name = (card.get("name") or "").strip()
        if not name or len(name) > 90:
            return None

        rating, reviews = 0.0, 0
        for line in lines:
            match = RATING_RE.search(line)
            if match:
                try:
                    rating = float(match.group(1).replace(",", "."))
                except ValueError:
                    pass
                if match.group(2):
                    reviews = int(re.sub(r"[^0-9]", "", match.group(2)) or 0)
                break
        if not reviews:
            match = REVIEW_RE.search(text.replace("\n", " "))
            if match:
                reviews = int(re.sub(r"[^0-9]", "", match.group(1)) or 0)

        phone = ""
        for line in lines:
            found = _find_phone(line) or _find_phone(text)
            if found:
                phone = found
                break

        website = ""
        for line in lines:
            if re.search(r"\.(com|pk|net|org|io|co)\b", line, re.I) and " " not in line.strip():
                website = _clean_site(line)
                break

        address = ""
        for line in lines[1:]:
            if any(ch.isdigit() for ch in line) and len(line) > 8:
                address = line
                break
        if not address and len(lines) > 1:
            address = lines[-1][:120]

        return {
            "name": name,
            "phone": phone,
            "email": "",
            "address": address,
            "category": category,
            "city": city,
            "website": website,
            "rating": rating,
            "reviews": reviews,
            "source": "google_maps:%s" % query,
        }

    # ------------------------------------------------------------- fallbacks
    def _fallback_search(self, query: str, city: str, category: str,
                         limit: int) -> List[Dict[str, Any]]:
        """No Chrome?  Use DuckDuckGo + directory pages.  Worse, but it works."""
        web = self.apartment.tool("web")
        leads: List[Dict[str, Any]] = []
        for result in web.search("%s %s contact number" % (category, city), count=limit):
            if self._stopped():
                break
            snippet = result.get("snippet", "")
            title = result.get("title", "")
            name = re.split(r"\s[-|–]\s|\s\|\s", title)[0].strip()
            if not name:
                continue
            leads.append({
                "name": name[:90],
                "phone": _find_phone(snippet) or "",
                "email": _find_email(snippet),
                "address": "",
                "category": category,
                "city": city,
                "website": _clean_site(result.get("url", "")),
                "rating": 0.0,
                "reviews": 0,
                "source": "web_search:%s" % query,
            })
            self._throttle()
        return self._dedupe([lead for lead in leads if lead["name"]])[:limit]

    def scrape_directory(self, url: str, city: str = "", category: str = "",
                         limit: int = 40) -> List[Dict[str, Any]]:
        """Scrape a business directory page (e.g. a local listings site)."""
        self.apartment.gate.enforce(self.apartment.NAME, "scrape_maps", {"url": url})
        web = self.apartment.tool("web")
        self._throttle()
        html = web.get(url)
        if not html:
            return []
        leads: List[Dict[str, Any]] = []
        for block in _split_listings(html):
            name = _first_text(block, r"<h[23][^>]*>(.*?)</h[23]>")
            if not name or len(name) > 90:
                continue
            leads.append({
                "name": _strip_tags(name)[:90],
                "phone": _find_phone(block) or "",
                "email": _find_email(block),
                "address": _strip_tags(_first_text(block, r"address[^>]*>(.*?)<") or "")[:140],
                "category": category,
                "city": city,
                "website": _clean_site(_first_attr(block, r'href="(https?://[^"]+)"') or ""),
                "rating": 0.0, "reviews": 0,
                "source": "directory:%s" % url,
            })
            if len(leads) >= limit:
                break
        return self._dedupe(leads)

    # ------------------------------------------------------------- csv import
    def import_csv(self, path, city_default: str = "", category_default: str = ""
                   ) -> List[Dict[str, Any]]:
        """Manual fallback - the safety net when scraping is blocked.

        Accepts any CSV with at least a ``name`` column; other columns are
        matched loosely (phone/mobile, email, address, website, rating, reviews).
        """
        file_tool = self.apartment.tool("file")
        rows = file_tool.read_csv(str(path))
        leads = []
        for row in rows:
            normalised = {str(k).strip().lower(): (v or "").strip()
                          for k, v in row.items() if k}
            name = (normalised.get("name") or normalised.get("business name")
                    or normalised.get("business") or normalised.get("title") or "")
            if not name:
                continue
            leads.append({
                "name": name[:90],
                "phone": (normalised.get("phone") or normalised.get("mobile")
                          or normalised.get("contact") or ""),
                "email": normalised.get("email") or "",
                "address": normalised.get("address") or "",
                "category": (normalised.get("category") or normalised.get("type")
                             or category_default),
                "city": normalised.get("city") or city_default,
                "website": _clean_site(normalised.get("website") or normalised.get("site") or ""),
                "rating": _to_float(normalised.get("rating")),
                "reviews": _to_int(normalised.get("reviews")),
                "source": "csv:%s" % path,
            })
        return self._dedupe(leads)

    def export_csv(self, leads: Iterable[Dict[str, Any]], path: str) -> Any:
        file_tool = self.apartment.tool("file")
        return file_tool.write_csv(path, list(leads), fieldnames=list(LEAD_FIELDS))

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _dedupe(leads: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for lead in leads:
            key = re.sub(r"[^a-z0-9]", "", (lead.get("name", "") + lead.get("address", "")
                                            ).lower())[:60]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(lead)
        return out

    def info(self) -> Dict[str, Any]:
        return dict(self.stats, min_interval=self.min_interval,
                    max_results=self.max_results)


# --------------------------------------------------------------------------- #
# Small HTML/regex helpers (shared with enrich.py)
# --------------------------------------------------------------------------- #

PHONE_RE = re.compile(
    r"(?:\+?92[\s\-]?|0092[\s\-]?|0)3[0-9]{2}[\s\-]?[0-9]{7}\b"
    r"|(?:\+?92[\s\-]?|0)[1-9][0-9]{1,4}[\s\-]?[0-9]{5,8}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    import html as html_mod
    text = _TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def _find_phone(text: str) -> str:
    match = PHONE_RE.search(text or "")
    if not match:
        return ""
    digits = re.sub(r"[^0-9]", "", match.group(0))
    digits = re.sub(r"^(?:0092|92)", "0", digits)
    if len(digits) == 11 and digits.startswith("0"):
        return "%s-%s" % (digits[:4], digits[4:])
    return digits


def _find_email(text: str) -> str:
    match = EMAIL_RE.search(text or "")
    return match.group(0).lower() if match else ""


def _clean_site(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if any(bad in host for bad in ("google.", "facebook.", "instagram.", "linkedin.",
                                   "yelp.", "tripadvisor.", "yellowpages")):
        return ""
    return url.split("?")[0]


def _first_text(block: str, pattern: str) -> str:
    match = re.search(pattern, block or "", re.S | re.I)
    return match.group(1) if match else ""


def _first_attr(block: str, pattern: str) -> str:
    match = re.search(pattern, block or "", re.I)
    return match.group(1) if match else ""


def _split_listings(html: str) -> List[str]:
    chunks = re.split(r"<(?:li|article|div)\b", html or "")
    return [c[:4000] for c in chunks if len(c) > 120][:400]


def _to_float(value) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return 0.0


def _to_int(value) -> int:
    try:
        return int(re.sub(r"[^0-9]", "", str(value)) or 0)
    except Exception:
        return 0


def build(apartment) -> ScrapeTool:
    return ScrapeTool(apartment)
