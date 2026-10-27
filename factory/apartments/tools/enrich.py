"""Lead enrichment - six-layer email extraction, phones, socials, site status.

The brief suggests ``dataforge`` ("open-source Apollo alternative, 6-layer email
extraction") and ``coldreach`` ("free alternative to Hunter.io").  Reality check:

*   ``dataforge`` on PyPI is an unrelated University of Chicago packaging tool -
    the enrichment engine people mean is not published there.
*   ``coldreach`` is real and MIT licensed, but needs Python 3.11+ and drags in
    FastAPI, dnspython, email-validator... and its full stack wants Docker.

So the six layers are implemented here directly - the same techniques those
tools use (mailto, regex, Cloudflare decode, JSON-LD, obfuscation decode,
contact-page crawl), in about 200 lines, with zero dependencies.  ``coldreach``
is wired as an optional tier-2 provider and used only if you install it.

Nothing here verifies emails by sending to them - that is what gets domains
blocked and looks like spam from the recipient's side.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from .scrape import EMAIL_RE, PHONE_RE, _clean_site, _find_phone

SOCIAL_PATTERNS = {
    "facebook": re.compile(r"facebook\.com/([A-Za-z0-9._\-]{3,})", re.I),
    "instagram": re.compile(r"instagram\.com/([A-Za-z0-9._\-]{3,})", re.I),
    "linkedin": re.compile(r"linkedin\.com/(?:company|in)/([A-Za-z0-9._\-]{3,})", re.I),
    "twitter": re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{3,})", re.I),
    "youtube": re.compile(r"youtube\.com/(?:c/|channel/|@)?([A-Za-z0-9._\-@]{3,})", re.I),
    "tiktok": re.compile(r"tiktok\.com/@([A-Za-z0-9._\-]{3,})", re.I),
    "whatsapp": re.compile(r"wa\.me/(?:link|\?)?([0-9]{8,15})", re.I),
}

CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about", "/about-us",
                 "/kontact", "/联系我们", "/lien-he", "/contact.html", "/contact.php")

#: Emails that belong to infrastructure, not to a human you can sell to.
NOISE_EMAILS = re.compile(
    r"^(?:noreply|no-reply|donotreply|postmaster|webmaster@|abuse@|hostmaster@|"
    r"admin@.*\.gov|.*sentry\.io$|.*@example\.(?:com|org)$|.*@domain\.com$|"
    r".*@email\.com$|.*@test\.)", re.I)

#: Image/asset filenames that regex extraction mistakes for emails.
ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|css|js|woff2?|ico)$", re.I)


class EnrichTool:
    """Turns a bare business name into a contactable lead."""

    def __init__(self, apartment):
        self.apartment = apartment
        cfg = apartment.config.get("limits", {}) or {}
        self.min_interval = float(cfg.get("request_interval_s", 2.0))
        self.max_pages = int(cfg.get("max_pages_per_lead", 3))
        self.max_bytes = int(cfg.get("max_bytes", 220_000))
        self.stats = {"fetched": 0, "emails": 0, "phones": 0, "socials": 0, "failures": 0}
        self._last = 0.0
        self._coldreach = None

    def _throttle(self) -> None:
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    # ------------------------------------------------------------------ layers
    def extract_emails(self, html: str, base_url: str = "") -> List[str]:
        """All six layers, cheapest first.  Returns de-duplicated, ranked emails."""
        found: List[str] = []
        found += self._layer1_mailto(html)
        found += self._layer2_regex(html)
        found += self._layer3_cloudflare(html)
        found += self._layer4_jsonld(html)
        found += self._layer5_obfuscated(html)
        return _rank_emails(found)

    @staticmethod
    def _layer1_mailto(html: str) -> List[str]:
        out = []
        for match in re.finditer(r"mailto:([^\"'\s>?]+)", html or "", re.I):
            email = match.group(1).split("?")[0].strip().lower()
            if "@" in email:
                out.append(email)
        return out

    @staticmethod
    def _layer2_regex(html: str) -> List[str]:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html or "",
                      flags=re.S | re.I)
        return [m.group(0).lower() for m in EMAIL_RE.finditer(text)]

    @staticmethod
    def _layer3_cloudflare(html: str) -> List[str]:
        """Cloudflare email protection: XOR the hex blob with its first byte."""
        out = []
        for match in re.finditer(r'data-cfemail="([0-9a-f]+)"', html or "", re.I):
            blob = match.group(1)
            try:
                key = int(blob[:2], 16)
                decoded = "".join(chr(int(blob[i:i + 2], 16) ^ key)
                                  for i in range(2, len(blob), 2))
            except Exception:
                continue
            if "@" in decoded:
                out.append(decoded.lower())
        return out

    @staticmethod
    def _layer4_jsonld(html: str) -> List[str]:
        """schema.org blocks carry the official contact email."""
        out = []
        for match in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                                 html or "", re.S | re.I):
            try:
                data = json.loads(match.group(1).strip())
            except Exception:
                continue
            out.extend(_walk_emails(data))
        return out

    @staticmethod
    def _layer5_obfuscated(html: str) -> List[str]:
        """``name [at] domain [dot] com`` and ``name(at)domain.com``."""
        text = re.sub(r"<[^>]+>", " ", html or "")
        text = (text.replace("[at]", "@").replace("(at)", "@").replace(" at ", "@")
                    .replace("[dot]", ".").replace("(dot)", ".").replace(" dot ", "."))
        return [m.group(0).lower() for m in EMAIL_RE.finditer(text)]

    def _layer6_contact_pages(self, base_url: str) -> List[str]:
        """Last resort: crawl the contact page too."""
        if not base_url or self.apartment.offline:
            return []
        web = self.apartment.tool("web")
        out: List[str] = []
        for path in CONTACT_PATHS[:4]:
            url = urljoin(base_url if base_url.endswith("/") else base_url + "/",
                          path.lstrip("/"))
            if not web.allowed(url):
                continue
            self._throttle()
            html = web.get(url, max_bytes=self.max_bytes)
            if not html:
                continue
            out.extend(self._layer1_mailto(html))
            out.extend(self._layer2_regex(html))
            if out:
                break
        return out

    # ------------------------------------------------------------------ phones
    @staticmethod
    def extract_phones(text: str) -> List[str]:
        out = []
        for match in PHONE_RE.finditer(text or ""):
            digits = re.sub(r"[^0-9]", "", match.group(0))
            digits = re.sub(r"^(?:0092|92)", "0", digits)
            if len(digits) == 11 and digits.startswith("0"):
                digits = "%s-%s" % (digits[:4], digits[4:])
            if digits not in out:
                out.append(digits)
        return out

    @staticmethod
    def extract_socials(html: str) -> Dict[str, str]:
        socials: Dict[str, str] = {}
        for name, pattern in SOCIAL_PATTERNS.items():
            match = pattern.search(html or "")
            if match:
                handle = match.group(1) if match.groups() else match.group(0)
                handle = handle.strip("/@")
                if handle and handle.lower() not in ("sharer", "share", "plugins", "tr"):
                    socials[name] = handle
        return socials

    # ------------------------------------------------------------------ checks
    def check_website_exists(self, url: str) -> Dict[str, Any]:
        """Is the site up?  Returns status, final URL, and a note when parked."""
        if not url:
            return {"exists": False, "status": "no_url"}
        if self.apartment.offline:
            return {"exists": False, "status": "offline_mode", "url": url}
        web = self.apartment.tool("web")
        self._throttle()
        html = web.get(url, max_bytes=40_000)
        if html is None:
            return {"exists": False, "status": "unreachable", "url": url}
        lowered = html.lower()
        parked = any(phrase in lowered for phrase in (
            "domain for sale", "buy this domain", "coming soon", "under construction",
            "this domain is parked", "website is under construction"))
        return {"exists": True, "status": "parked" if parked else "live",
                "url": url, "bytes": len(html)}

    # -------------------------------------------------------------- enrichment
    def enrich(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch a lead's site (if any) and fill in email/phones/socials/status."""
        website = _clean_site(lead.get("website", ""))
        result = {
            "email": lead.get("email", "") or "",
            "phones": [lead["phone"]] if lead.get("phone") else [],
            "socials": lead.get("socials") or {},
            "site_status": "unknown",
            "pages_checked": 0,
        }
        if not website:
            result["site_status"] = "no_website"
            return result

        status = self.check_website_exists(website)
        result["site_status"] = status.get("status", "unknown")
        if not status.get("exists"):
            return result

        web = self.apartment.tool("web")
        self._throttle()
        html = web.get(website, max_bytes=self.max_bytes) or ""
        result["pages_checked"] = 1

        emails = self.extract_emails(html, website)
        if not emails and self.max_pages > 1:
            emails = self._layer6_contact_pages(website)
            result["pages_checked"] += 1
        if not emails and self._coldreach_available():
            emails = self._coldreach_emails(website)

        result["email"] = emails[0] if emails else ""
        result["all_emails"] = emails[:5]
        for phone in self.extract_phones(html)[:3]:
            if phone not in result["phones"]:
                result["phones"].append(phone)
        socials = self.extract_socials(html)
        if socials:
            merged = dict(result["socials"])
            merged.update(socials)
            result["socials"] = merged

        # deliverability: is this address/phone actually reachable?
        result["verify"] = self.verify_lead(emails, result["phones"], website)
        if result["verify"].get("best_email") and not result["email"]:
            result["email"] = result["verify"]["best_email"]
        if result["verify"].get("best_phone") and not result["phones"]:
            result["phones"] = [result["verify"]["best_phone"]]

        self.stats["fetched"] += 1
        self.stats["emails"] += 1 if result["email"] else 0
        self.stats["phones"] += len(result["phones"])
        self.stats["socials"] += len(result["socials"])
        self.apartment.log("enrich", lead=lead.get("name", ""), site=website,
                           email=result["email"], status=result["site_status"])
        return result

    # ------------------------------------------------------------ deliverability
    def verify_lead(self, emails: Optional[Sequence[str]] = None,
                    phones: Optional[Sequence[str]] = None,
                    website: str = "") -> Dict[str, Any]:
        """Score every contact path so outreach never wastes a slot on a dud.

        Pure-Python/no-API: MX lookup over a hand-rolled DNS client, PK mobile
        prefix rules for WhatsApp eligibility, disposable/role-mailbox lists.
        """
        try:
            verify = self.apartment.tool("verify")
        except Exception:
            return {"available": False}
        report = verify.score_lead(emails or [], phones or [], website)
        report["available"] = True
        self.apartment.log(
            "verify", emails=len(report.get("emails") or []),
            sendable=len(report.get("sendable_emails") or []),
            whatsapp=len(report.get("whatsapp_numbers") or []))
        return report

    # ------------------------------------------------------- optional tier two
    def _coldreach_available(self) -> bool:  # pragma: no cover
        if self._coldreach is not None:
            return True
        try:
            import coldreach  # type: ignore
            self._coldreach = coldreach
            return True
        except Exception:
            return False

    def _coldreach_emails(self, domain: str) -> List[str]:  # pragma: no cover
        """Use ColdReach when installed (Python 3.11+).  Silently ignored if not."""
        try:
            finder = getattr(self._coldreach, "find_emails", None)
            if finder:
                return [str(e).lower() for e in finder(domain)]
        except Exception as exc:
            self.apartment.log("coldreach_failed", error=str(exc))
        return []

    def info(self) -> Dict[str, Any]:
        return dict(self.stats, coldreach=self._coldreach is not None)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _walk_emails(node: Any, depth: int = 0) -> List[str]:
    """Recursively pull email strings out of parsed JSON-LD."""
    out: List[str] = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and "@" in value and EMAIL_RE.search(value):
                out.extend(m.group(0).lower() for m in EMAIL_RE.finditer(value))
            else:
                out.extend(_walk_emails(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_emails(item, depth + 1))
    return out


def _rank_emails(emails: Iterable[str]) -> List[str]:
    """Prefer a real human address; drop assets, noise and duplicates."""
    scored: Dict[str, int] = {}
    for email in emails:
        email = (email or "").strip().strip(".,;'\"<>()").lower()
        if not email or len(email) > 80 or "@" not in email:
            continue
        if ASSET_RE.search(email) or NOISE_EMAILS.search(email):
            continue
        local, _, domain = email.partition("@")
        if "." not in domain or len(domain) < 4:
            continue
        score = 0
        if local in ("info", "contact", "hello", "sales", "admin", "office"):
            score += 3
        if local in ("support", "help", "enquiries", "inquiries", "bookings"):
            score += 2
        if domain.endswith((".com", ".pk", ".net", ".org", ".co", ".io")):
            score += 1
        if any(bad in domain for bad in ("sentry", "wixpress", "godaddy", "squarespace",
                                         "googleapis", "gstatic", "cloudflare")):
            score -= 5
        scored[email] = max(scored.get(email, 0), score)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [email for email, score in ranked if score > -3]


def build(apartment) -> EnrichTool:
    return EnrichTool(apartment)
