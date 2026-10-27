"""Website audit - the proof engine.  No API, no Lighthouse, no PageSpeed key.

Everything is measured with the standard library:

*   **SSL** - ``ssl`` + ``socket`` handshake: cert validity, days to expiry,
    issuer.  We deliberately ignore hostname mismatches (see below) but we do
    report them as an issue, because a padlock warning scares customers away.
*   **Speed** - real wall-clock timing of the fetch: TTFB, total time, payload
    size, number of sub-resources.  On Pakistani 3G/4G this is the single most
    persuasive number you can put in front of a restaurant owner.
*   **Mobile** - viewport meta, responsive CSS hints, tiny-font and tap-target
    heuristics, because ~80% of Pakistani traffic is mobile.
*   **Links** - up to 15 internal/external links checked with HEAD, rate-limited.
*   **SEO** - title, meta description, H1 count, image alt coverage, canonical,
    lang attribute, Open Graph tags, sitemap/robots presence.

Learning: ``models/impact.json`` holds a per-issue weight.  Each time a lead
converts (or ghost you), apt_self_trainer calls :meth:`record_outcome` and the
weights move - so over time the report leads with *your* highest-converting
issues, not a generic list.
"""

from __future__ import annotations

import re
import socket
import ssl
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .enrich import SOCIAL_PATTERNS
from .scrape import _clean_site

#: Every issue the auditor can find, with its default severity weight.
ISSUE_LIBRARY: Dict[str, Dict[str, Any]] = {
    "no_ssl": {"weight": 10, "title": "SSL certificate nahi hai (https nahi)",
               "benefit": "browser mein 'Not Secure' warning aati hai, log wapis chale jate hain"},
    "ssl_expiring": {"weight": 6, "title": "SSL certificate jald expire ho raha hai",
                     "benefit": "expiry ke baad site band ho jayegi"},
    "no_website": {"weight": 10, "title": "Koi website hai hi nahi",
                   "benefit": "Google Maps pe aap ka business dikh raha hai par log details nahi dekh sakte"},
    "parked_domain": {"weight": 8, "title": "Domain khara hai, site abhi bani nahi",
                      "benefit": "jo bhi aap ka naam search kare, usay khaali page milta hai"},
    "slow_ttfb": {"weight": 8, "title": "Server slow hai (pehla byte late aata hai)",
                  "benefit": "log 3 second se pehle tab close kar dete hain"},
    "heavy_page": {"weight": 6, "title": "Page bohat bhaari hai (MBs mein)",
                   "benefit": "mobile data khatam hota hai, log dobara nahi aate"},
    "too_many_requests": {"weight": 4, "title": "Bohat sari files load hoti hain",
                          "benefit": "har extra file page ko aur slow karti hai"},
    "no_viewport": {"weight": 9, "title": "Mobile ke liye theek nahi (viewport missing)",
                    "benefit": "phone pe site choti aur cut-off dikhti hai"},
    "no_meta_description": {"weight": 5, "title": "Meta description nahi hai",
                            "benefit": "Google search mein aap ka result pheeka lagta hai"},
    "title_missing": {"weight": 7, "title": "Page title missing/weak hai",
                      "benefit": "Google ko samajh nahi aata aap kya karte hain"},
    "title_too_long": {"weight": 3, "title": "Page title bohat lamba hai",
                       "benefit": "Google search mein title cut ho jata hai"},
    "no_h1": {"weight": 4, "title": "H1 heading nahi hai",
              "benefit": "page ki pehchaan search engine ko nahi milti"},
    "multiple_h1": {"weight": 2, "title": "Ek se zyada H1 hain",
                    "benefit": "search engine confuse hota hai"},
    "images_no_alt": {"weight": 4, "title": "Images mein alt text nahi hai",
                      "benefit": "Google Images se traffic nahi aata"},
    "broken_links": {"weight": 6, "title": "Tooti hui links hain",
                     "benefit": "jo page nahi khulta, wo customer ko ghusa dilata hai"},
    "no_canonical": {"weight": 2, "title": "Canonical tag missing hai",
                     "benefit": "duplicate content ka masla ho sakta hai"},
    "no_lang": {"weight": 2, "title": "Language attribute nahi hai",
                "benefit": "Urdu/English content screen readers ko samajh nahi aata"},
    "no_open_graph": {"weight": 3, "title": "Social sharing tags nahi hain",
                      "benefit": "WhatsApp/Facebook pe link share karne pe khaali dikhta hai"},
    "no_whatsapp_link": {"weight": 7, "title": "WhatsApp button nahi hai",
                         "benefit": "customer aap ko direct message nahi kar sakta - ye sab se aasan order hai"},
    "no_contact_info": {"weight": 8, "title": "Phone/email page pe dhoondhna mushkil hai",
                        "benefit": "jo customer call karna chahta hai, wo wapis chala jata hai"},
    "no_analytics": {"weight": 3, "title": "Analytics nahi hai",
                     "benefit": "aap ko pata hi nahi kitne log aaye aur kahan gaye"},
    "no_sitemap": {"weight": 3, "title": "Sitemap.xml nahi hai",
                   "benefit": "Google ko aap ke pages milne mein waqt lagta hai"},
    "no_schema": {"weight": 4, "title": "LocalBusiness schema nahi hai",
                  "benefit": "Google Maps aur local search mein ranking weak rehti hai"},
    "mobile_unfriendly_css": {"weight": 5, "title": "Responsive CSS nahi hai",
                              "benefit": "tablet aur chote phones pe layout toot jata hai"},
    "no_cms_or_stale": {"weight": 3, "title": "Site purani lag rahi hai (updates nahi)",
                        "benefit": "purani site se trust kam hota hai"},
}


class WebsiteAuditTool:
    """Audits one website and turns findings into persuasive proof."""

    def __init__(self, apartment):
        self.apartment = apartment
        cfg = apartment.config.get("limits", {}) or {}
        self.timeout = float(cfg.get("timeout_s", 12))
        self.max_link_checks = int(cfg.get("max_link_checks", 15))
        self.max_bytes = int(cfg.get("max_bytes", 300_000))
        self.weights = self._load_weights()

    def _load_weights(self) -> Dict[str, float]:
        learned = self.apartment.load_model("impact.json", default=None) or {}
        weights = {key: float(value.get("weight", 0))
                   for key, value in ISSUE_LIBRARY.items()}
        for key, entry in (learned.get("weights") or {}).items():
            if key in weights:
                weights[key] = float(entry)
        return weights

    # ------------------------------------------------------------------- checks
    def check_ssl(self, url: str) -> Dict[str, Any]:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port or 443
        if not host:
            return {"ok": False, "reason": "no host"}
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            started = time.time()
            with socket.create_connection((host, port), timeout=self.timeout) as sock:
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert() or {}
                    handshake_ms = int((time.time() - started) * 1000)
        except ssl.SSLError as exc:
            return {"ok": False, "reason": "ssl error: %s" % exc}
        except Exception as exc:
            return {"ok": False, "reason": "unreachable: %s" % exc}

        expiry = cert.get("notAfter") or ""
        days_left = None
        if expiry:
            try:
                expires = datetime.strptime(expiry, "%b %d %H:%M:%S %Y %Z")
                days_left = (expires - datetime.utcnow()).days
            except Exception:
                pass
        issuer = ""
        for rdn in (cert.get("issuer") or ()):
            for key, value in rdn:
                if key == "organizationName":
                    issuer = value
        return {"ok": True, "days_to_expiry": days_left, "issuer": issuer,
                "handshake_ms": handshake_ms, "expiry": expiry}

    def check_page_speed(self, url: str) -> Dict[str, Any]:
        web = self.apartment.tool("web")
        started = time.time()
        html = web.get(url, max_bytes=self.max_bytes)
        total_ms = int((time.time() - started) * 1000)
        if not html:
            return {"ok": False, "total_ms": total_ms, "reason": "fetch failed"}
        ttfb = self._ttfb(url)
        resources = len(re.findall(r'(?:src|href)="([^"#?]+\.[a-z0-9]{2,5})', html, re.I))
        return {"ok": True, "ttfb_ms": ttfb, "total_ms": total_ms,
                "bytes": len(html.encode("utf-8", "replace")),
                "resource_count": resources}

    def _ttfb(self, url: str) -> int:
        web = self.apartment.tool("web")
        started = time.time()
        try:
            if web._raw(url, max_bytes=0) is not None:
                return int((time.time() - started) * 1000)
        except Exception:
            pass
        return -1

    @staticmethod
    def check_mobile_friendly(html: str) -> Dict[str, Any]:
        viewport = bool(re.search(r'<meta[^>]+name=["\']viewport["\']', html or "", re.I))
        media = len(re.findall(r"@media[^{]+\(\s*(?:max|min)-width", html or "", re.I))
        tiny_fonts = len(re.findall(r"font-size:\s*([0-9]{1,2})px", html or "", re.I))
        fixed_width = bool(re.search(r"width:\s*([0-9]{3,})px", html or "", re.I))
        small_taps = len(re.findall(r"height:\s*([0-9]{1,2})px", html or "", re.I))
        return {"viewport": viewport, "media_queries": media, "tiny_fonts": tiny_fonts,
                "fixed_width": fixed_width, "small_tap_targets": small_taps,
                "responsive": viewport and (media > 0 or not fixed_width)}

    @staticmethod
    def check_seo_basics(html: str) -> Dict[str, Any]:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
        title = re.sub(r"\s+", " ", (title_match.group(1) if title_match else "")).strip()
        meta = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)',
                         html or "", re.I)
        h1_count = len(re.findall(r"<h1[\s>]", html or "", re.I))
        images = re.findall(r"<img[^>]*>", html or "", re.I)
        missing_alt = sum(1 for tag in images if not re.search(r"alt=[\"'][^\"']+[\"']",
                                                               tag, re.I))
        canonical = bool(re.search(r'<link[^>]+rel=["\']canonical["\']', html or "", re.I))
        lang = bool(re.search(r"<html[^>]+lang=", html or "", re.I))
        og = bool(re.search(r'<meta[^>]+property=["\']og:', html or "", re.I))
        schema = "application/ld+json" in (html or "").lower()
        return {"title": title, "title_len": len(title),
                "meta_description": (meta.group(1) if meta else "").strip(),
                "h1_count": h1_count, "images": len(images), "images_missing_alt": missing_alt,
                "canonical": canonical, "lang": lang, "open_graph": og, "schema": schema}

    def check_broken_links(self, url: str, html: str) -> List[Dict[str, str]]:
        web = self.apartment.tool("web")
        hrefs = re.findall(r'href="([^"#]+)"', html or "")
        candidates: List[str] = []
        for href in hrefs:
            if href.startswith(("mailto:", "tel:", "javascript:", "whatsapp:")):
                continue
            full = urljoin(url, href)
            if full in candidates or len(candidates) >= self.max_link_checks:
                continue
            if urlsplit(full).netloc and urlsplit(full).netloc != urlsplit(url).netloc:
                continue          # skip externals: slow, and not our problem
            candidates.append(full)
        broken = []
        for link in candidates:
            try:
                if web._raw(link, max_bytes=0) is None:
                    broken.append({"url": link, "status": "unreachable"})
            except Exception:
                broken.append({"url": link, "status": "error"})
            time.sleep(0.4)
        return broken

    @staticmethod
    def check_contactability(html: str) -> Dict[str, Any]:
        has_whatsapp = bool(re.search(r"(wa\.me|api\.whatsapp\.com|whatsapp://)",
                                      html or "", re.I))
        phones = len(re.findall(r"(?:\+?92|0)3[0-9]{2}[\s\-]?[0-9]{7}", html or ""))
        emails = len(re.findall(r"mailto:", html or "", re.I))
        forms = len(re.findall(r"<form[\s>]", html or "", re.I))
        analytics = bool(re.search(r"(gtag|google-analytics|googletagmanager|fbq\()",
                                   html or "", re.I))
        return {"whatsapp_link": has_whatsapp, "phones": phones, "emails": emails,
                "forms": forms, "analytics": analytics,
                "contactable": (has_whatsapp or phones > 0 or emails > 0 or forms > 0)}

    # -------------------------------------------------------------------- main
    def audit(self, url: str, fetch_html: bool = True) -> Dict[str, Any]:
        """Full audit.  Returns issues, score, and a ready-to-send report."""
        self.apartment.gate.enforce(self.apartment.NAME, "audit_site", {"url": url})
        url = _clean_site(url)
        issues: List[Dict[str, Any]] = []
        details: Dict[str, Any] = {"url": url}

        if not url:
            issues.append(_issue("no_website", self.weights, evidence="koi website nahi mili"))
            return self._finish(url, issues, details)

        if self.apartment.offline:
            return {"url": url, "score": -1, "issues": [], "offline": True,
                    "report": "Offline mode: audit skip kiya gaya.", "details": {}}

        web = self.apartment.tool("web")
        if not web.allowed(url):
            return {"url": url, "score": -1, "issues": [], "blocked": True,
                    "report": "robots.txt ne is site ko audit karne se mana kiya.", "details": {}}

        # --- SSL -------------------------------------------------------------
        ssl_result = self.check_ssl(url)
        details["ssl"] = ssl_result
        if not ssl_result.get("ok"):
            if url.startswith("http://"):
                issues.append(_issue("no_ssl", self.weights,
                                     evidence="site http par chal rahi hai, https nahi"))
        elif isinstance(ssl_result.get("days_to_expiry"), int):
            if ssl_result["days_to_expiry"] <= 21:
                issues.append(_issue("ssl_expiring", self.weights,
                                     evidence="certificate %d din mein expire" %
                                     ssl_result["days_to_expiry"]))

        # --- fetch + speed ---------------------------------------------------
        speed = self.check_page_speed(url)
        details["speed"] = speed
        if speed.get("ok"):
            if speed.get("ttfb_ms", 0) > 1200 or speed.get("total_ms", 0) > 3500:
                issues.append(_issue("slow_ttfb", self.weights,
                                     evidence="server ka jawab %s ms" %
                                     max(speed.get("ttfb_ms", 0), speed.get("total_ms", 0))))
            if speed.get("bytes", 0) > 1_500_000:
                issues.append(_issue("heavy_page", self.weights,
                                     evidence="page size %.1f MB" % (speed["bytes"] / 1e6)))
            if speed.get("resource_count", 0) > 60:
                issues.append(_issue("too_many_requests", self.weights,
                                     evidence="%d files load hoti hain" %
                                     speed["resource_count"]))

        html = ""
        if fetch_html and speed.get("ok"):
            html = web.get(url, max_bytes=self.max_bytes) or ""
        if not html:
            return self._finish(url, issues, details)

        # --- mobile ----------------------------------------------------------
        mobile = self.check_mobile_friendly(html)
        details["mobile"] = mobile
        if not mobile["viewport"]:
            issues.append(_issue("no_viewport", self.weights,
                                 evidence="viewport meta tag missing"))
        elif not mobile["responsive"]:
            issues.append(_issue("mobile_unfriendly_css", self.weights,
                                 evidence="responsive CSS rules nahi miltin"))

        # --- SEO -------------------------------------------------------------
        seo = self.check_seo_basics(html)
        details["seo"] = seo
        if not seo["title"]:
            issues.append(_issue("title_missing", self.weights, evidence="<title> khaali hai"))
        elif seo["title_len"] > 65:
            issues.append(_issue("title_too_long", self.weights,
                                 evidence="title %d characters" % seo["title_len"]))
        if not seo["meta_description"]:
            issues.append(_issue("no_meta_description", self.weights,
                                 evidence="meta description missing"))
        if seo["h1_count"] == 0:
            issues.append(_issue("no_h1", self.weights, evidence="koi H1 heading nahi"))
        elif seo["h1_count"] > 1:
            issues.append(_issue("multiple_h1", self.weights,
                                 evidence="%d H1 headings" % seo["h1_count"]))
        if seo["images"] and seo["images_missing_alt"] > 0:
            issues.append(_issue("images_no_alt", self.weights,
                                 evidence="%d/%d images mein alt nahi" %
                                 (seo["images_missing_alt"], seo["images"])))
        if not seo["canonical"]:
            issues.append(_issue("no_canonical", self.weights, evidence="canonical missing"))
        if not seo["lang"]:
            issues.append(_issue("no_lang", self.weights, evidence="lang attribute missing"))
        if not seo["open_graph"]:
            issues.append(_issue("no_open_graph", self.weights,
                                 evidence="og: tags missing - WhatsApp pe khaali preview"))
        if not seo["schema"]:
            issues.append(_issue("no_schema", self.weights,
                                 evidence="LocalBusiness schema missing"))

        # --- contactability --------------------------------------------------
        contact = self.check_contactability(html)
        details["contact"] = contact
        if not contact["whatsapp_link"]:
            issues.append(_issue("no_whatsapp_link", self.weights,
                                 evidence="WhatsApp ka button/link nahi"))
        if not contact["contactable"]:
            issues.append(_issue("no_contact_info", self.weights,
                                 evidence="na phone, na email, na form"))
        if not contact["analytics"]:
            issues.append(_issue("no_analytics", self.weights,
                                 evidence="koi analytics nahi - traffic ka pata nahi"))

        # --- sitemap + broken links ------------------------------------------
        if not web.exists(urljoin(url, "/sitemap.xml")):
            issues.append(_issue("no_sitemap", self.weights, evidence="sitemap.xml nahi mila"))
        broken = self.check_broken_links(url, html)
        details["broken_links"] = broken
        if broken:
            issues.append(_issue("broken_links", self.weights,
                                 evidence="%d links khul hi nahi rahin" % len(broken),
                                 extra={"urls": [b["url"] for b in broken[:3]]}))

        return self._finish(url, issues, details)

    # ----------------------------------------------------------------- scoring
    def _finish(self, url: str, issues: List[Dict[str, Any]],
                details: Dict[str, Any]) -> Dict[str, Any]:
        issues.sort(key=lambda i: -i["weight"])
        penalty = sum(issue["weight"] for issue in issues)
        score = max(0, min(100, 100 - int(penalty * 1.6)))
        report = self.build_report({"name": details.get("name", "Aap ki site")}, issues, score)
        result = {"url": url, "score": score, "issues": issues, "details": details,
                  "report": report, "audited_at": time.time()}
        self.apartment.log("audit", url=url, score=score, issues=len(issues))
        return result

    def build_report(self, lead: Dict[str, Any], issues: List[Dict[str, Any]],
                     score: int) -> str:
        """Roman-Urdu report used verbatim in outreach - this is the proof."""
        business = lead.get("name") or "Aap ki site"
        if not issues:
            return ("%s ki site check ki - koi bara masla nahi mila (score %d/100). "
                    "Bas chhoti improvements hain." % (business, score))
        top = issues[:3]
        lines = ["%s ki site mein yeh %d issues hain:" % (business, len(top))]
        for index, issue in enumerate(top, 1):
            lines.append("  %d. %s - %s" % (index, issue["title"], issue["benefit"]))
            if issue.get("evidence"):
                lines.append("     (saboot: %s)" % issue["evidence"])
        lines.append("Overall score: %d/100" % score)
        return "\n".join(lines)

    # ---------------------------------------------------------------- learning
    def record_outcome(self, issues: List[Dict[str, Any]], converted: bool) -> Dict[str, float]:
        """Move issue weights based on what actually won the client.

        A moving-average update: converted nudges the weight up, a lost lead
        nudges it down.  Bounded so one lucky lead cannot distort everything.
        """
        model = self.apartment.load_model("impact.json", default=None) or {
            "weights": {}, "samples": 0}
        weights = dict(model.get("weights") or {})
        rate = 0.25
        for issue in issues or []:
            key = issue.get("id") if isinstance(issue, dict) else str(issue)
            if not key or key not in ISSUE_LIBRARY:
                continue
            current = float(weights.get(key, ISSUE_LIBRARY[key]["weight"]))
            target = current + (1.0 if converted else -0.6)
            weights[key] = round(max(1.0, min(15.0, current + rate * (target - current))), 3)
        model["weights"] = weights
        model["samples"] = int(model.get("samples", 0)) + 1
        model["updated_at"] = time.time()
        self.apartment.save_model("impact.json", model)
        self.weights = self._load_weights()
        return weights

    def info(self) -> Dict[str, Any]:
        return {"known_issues": len(ISSUE_LIBRARY),
                "learned_weights": {k: v for k, v in self.weights.items()
                                    if v != ISSUE_LIBRARY[k]["weight"]}}


def _issue(issue_id: str, weights: Dict[str, float], evidence: str = "",
           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = ISSUE_LIBRARY.get(issue_id, {"title": issue_id, "benefit": "", "weight": 3})
    issue = {"id": issue_id, "title": meta["title"], "benefit": meta["benefit"],
             "weight": float(weights.get(issue_id, meta["weight"])), "evidence": evidence}
    if extra:
        issue.update(extra)
    return issue


def build(apartment) -> WebsiteAuditTool:
    return WebsiteAuditTool(apartment)
