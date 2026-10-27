"""apt_platform_github - GitHub as a lead source and as free demo hosting.

Two jobs, both keyless:

1. **Lead source.**  Pakistani companies and devs publish repos with
   ``karachi``/``lahore``/``islamabad`` in the bio, README or location.  A repo
   with a **dead link, no README, a broken demo URL or a stale site** is a
   concrete, provable finding - the same proof-first opener the rest of the
   factory uses.  Discovery goes through ``tools/web.py`` (DuckDuckGo +
   ``github.com`` search pages) and the GitHub tool; no PAT, no API key,
   ``git-credential-oauth`` for anything authenticated.

2. **Free hosting.**  A demo built by apt_builder can be pushed to a GitHub
   Pages repo, which turns "here is a screenshot" into "here is a live URL" -
   the single strongest proof you can send a client.  Push is repo-allowlisted
   and approval-gated (MEDIUM risk).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

DAILY_CAP = 30
PAGES_REPO_PREFIX = "clienthunter-demo-"

CITY_TERMS = ("karachi", "lahore", "islamabad", "rawalpindi", "faisalabad",
              "multan", "peshawar", "quetta", "sialkot", "pakistan")

#: Signals that a repo's owner probably needs web work.
WEAK_SIGNALS = (
    (r"\bno readme\b", "README missing"),
    (r"coming soon", "site still says 'coming soon'"),
    (r"\bunder construction\b", "site under construction"),
    (r"localhost|127\.0\.0\.1", "demo link points at localhost"),
    (r"\bTODO\b", "unfinished TODOs in the public repo"),
    (r"last commit.*20(1\d|2[0-2])", "repo not touched in years"),
)


class PlatformGitHubAgent(BaseApartment):
    NAME = "platform_github"
    DESCRIPTION = "GitHub lead discovery (keyless) + GitHub Pages hosting for demos"
    PERMISSIONS = {"internet": True, "browser": True, "github": True,
                   "email": False, "whatsapp": False}

    STATE = "github_state.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        model = {
            "platform": "github",
            "daily_cap": DAILY_CAP,
            "city_terms": list(CITY_TERMS),
            "weak_signals": [{"pattern": pattern, "finding": finding}
                             for pattern, finding in WEAK_SIGNALS],
            "pages_repo_prefix": PAGES_REPO_PREFIX,
            "search_templates": [
                'site:github.com "{city}" {niche}',
                'github.com "{city}" {niche} website',
                'site:github.io "{city}" {niche}',
            ],
            "opener_style": "specific repo finding, then the offer",
            "rules": [
                "Never open with 'we build websites' - cite the repo finding.",
                "Only contact publicly listed emails/websites found in the repo.",
                "Push = approval-gated and repo-allowlisted.",
            ],
        }
        self.save_model("github_playbook.json", model)
        self.save_model(self.STATE, {"actions_today": 0, "day": "", "repos_seen": [],
                                     "leads_found": 0, "pages_published": [], "last_run": 0.0})
        self.log("trained", signals=len(WEAK_SIGNALS))
        return {"signals": len(WEAK_SIGNALS), "cap": DAILY_CAP}

    def _state(self) -> Dict[str, Any]:
        from datetime import datetime
        state = self.load_model(self.STATE, default={}) or {}
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("day") != today:
            state["actions_today"] = 0
            state["day"] = today
        return state

    def _save(self, state: Dict[str, Any]) -> None:
        import time as _t
        state["last_run"] = _t.time()
        self.save_model(self.STATE, state)

    # -------------------------------------------------------------- discovery
    def search(self, city: str = "Karachi", niche: str = "restaurant",
               limit: int = 10) -> List[Dict[str, Any]]:
        web = self.tool("web")
        model = self.load_model("github_playbook.json", default={}) or {}
        found: List[Dict[str, Any]] = []
        seen = set()
        for template in model.get("search_templates", [
                'site:github.com "{city}" {niche}']):
            query = template.format(city=city, niche=niche)
            try:
                results = web.search(query, limit=limit) or []
            except Exception:
                results = []
            for result in results:
                url = result.get("url", "")
                key = url.split("?")[0]
                if key in seen or "github.com" not in url:
                    continue
                seen.add(key)
                found.append({"platform": "github", "url": key,
                              "title": (result.get("title") or "")[:140],
                              "snippet": (result.get("snippet") or "")[:300],
                              "city": city, "category": niche})
            time.sleep(2.0)
            if len(found) >= limit:
                break
        self.log("repos_found", city=city, niche=niche, count=len(found))
        state = self._state()
        state.setdefault("repos_seen", []).extend(f["url"] for f in found)
        state["repos_seen"] = state["repos_seen"][-500:]
        self._save(state)
        return found[:limit]

    def audit_repo(self, url: str) -> Dict[str, Any]:
        """Fetch a repo page and look for public, provable weaknesses."""
        web = self.tool("web")
        findings: List[str] = []
        links: List[str] = []
        try:
            page = web.fetch(url, timeout=15.0) or ""
        except Exception as exc:
            return {"url": url, "findings": [], "error": str(exc)[:120], "links": []}
        text = re.sub(r"<[^>]+>", " ", page)
        text = re.sub(r"\s+", " ", text)
        for pattern, finding in WEAK_SIGNALS:
            if re.search(pattern, text, re.I):
                findings.append(finding)
        for match in re.findall(r"https?://(?!github\.com|www\.github\.com)[^\s\"'<>]+",
                                page)[:20]:
            if match not in links:
                links.append(match)
        return {"url": url, "findings": findings, "homepage": links[0] if links else "",
                "links": links[:8], "chars": len(page)}

    def to_leads(self, results: List[Dict[str, Any]],
                 audit_each: bool = True) -> List[Dict[str, Any]]:
        """Turn GitHub hits into store leads with a real, citable finding."""
        store = default_store()
        created = []
        for result in results:
            info = self.audit_repo(result["url"]) if audit_each else {}
            findings = (info or {}).get("findings") or []
            homepage = (info or {}).get("homepage") or ""
            name = result["title"].split("·")[0].split("|")[0].strip()[:80] or \
                result["url"].rstrip("/").split("/")[-1]
            lead_id = store.upsert_lead({
                "business_name": name,
                "city": result.get("city", ""),
                "category": result.get("category", ""),
                "website": homepage,
                "source": "github",
                "notes": "github=%s; findings=%s" % (result["url"], "; ".join(findings)),
                "status": "new",
            })
            if findings:
                store.update_fields(lead_id, notes="github=%s; findings=%s"
                                    % (result["url"], "; ".join(findings)))
                store.set_stage(lead_id, "audited")
                self.remember("github_audit:%s" % lead_id, info)
            created.append({"lead_id": lead_id, "name": name, "url": result["url"],
                            "findings": findings, "homepage": homepage})
            time.sleep(2.0)
        state = self._state()
        state["leads_found"] = int(state.get("leads_found", 0)) + len(created)
        self._save(state)
        self.log("leads_created", count=len(created))
        return created

    def compose(self, lead: Dict[str, Any], info: Optional[Dict[str, Any]] = None) -> str:
        info = info or (self.recall("github_audit:%s" % lead.get("id")) or {})
        findings = info.get("findings") or []
        name = (lead.get("business_name") or "your project").strip()
        if findings:
            return ("Salam! Aapka GitHub repo dekha (%s). Do cheezein notice hui: %s. "
                    "Agar aap chahein to main demo site bana kar GitHub Pages par live "
                    "kar doon - free, 24 ghante mein. Bataayein?" % (
                        (info.get("url") or "").split("/")[-1][:40],
                        ", ".join(findings[:2])))
        return ("Salam! Aapka repo dekha. Agar public demo/site chahiye to main GitHub "
                "Pages par live kar sakta hoon - pehle din se live link, koi cost nahi.")

    # ------------------------------------------------------------ pages hosting
    def publish_demo(self, lead: Dict[str, Any], folder: str) -> Dict[str, Any]:
        """Push a built demo folder to a GitHub Pages repo (approval-gated)."""
        github = self.tool("github")
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", (lead.get("business_name") or "demo").lower())
        slug = slug.strip("-") or "demo"
        repo = "%s%s" % (PAGES_REPO_PREFIX, slug[:40])
        try:
            result = github.publish_pages(repo, folder)
        except Exception as exc:
            return {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
        if result.get("ok"):
            state = self._state()
            state.setdefault("pages_published", []).append(
                {"lead_id": lead.get("id"), "repo": repo, "url": result.get("url", "")})
            self._save(state)
        return result

    def pages_url(self, lead: Dict[str, Any]) -> str:
        import re as _re
        github = self.tool("github")
        slug = _re.sub(r"[^a-z0-9]+", "-", (lead.get("business_name") or "demo").lower())
        slug = slug.strip("-") or "demo"
        return github.pages_url_for("%s%s" % (PAGES_REPO_PREFIX, slug[:40]))

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        if any(word in lowered for word in ("dhoondo", "find", "search", "scrape")):
            city, niche = self._parse(lowered)
            results = self.search(city, niche, limit=10)
            if not results:
                return "GitHub par kuch nahi mila '%s %s' ke liye." % (city, niche)
            return "\n".join("%s\n  %s" % (r["title"][:80], r["url"]) for r in results[:8])
        if any(word in lowered for word in ("leads", "banao", "add", "store")):
            city, niche = self._parse(lowered)
            results = self.search(city, niche, limit=8)
            created = self.to_leads(results)
            if not created:
                return "Koi lead nahi bana."
            return "%d leads bane:\n" % len(created) + "\n".join(
                "  #%s %s - %s" % (c["lead_id"], c["name"][:40],
                                   ", ".join(c["findings"][:2]) or "no finding")
                for c in created)
        if any(word in lowered for word in ("publish", "pages", "live", "host")):
            store = default_store()
            lead_id = (context or {}).get("lead_id")
            lead = store.get_lead(int(lead_id)) if lead_id else (
                store.leads(stage="demo_built", limit=1) or store.leads(limit=1) or [None])[0]
            if not lead:
                return "Pehle koi demo build karein (apt_builder)."
            folder = str(self.sandbox_path("demos/%s" % lead.get("id")).parent.parent)
            return str(self.publish_demo(lead, folder))
        state = self._state()
        return ("GitHub agent ready. Aaj %d actions. Leads found: %d. Pages published: %d.\n"
                "Kaam: 'github par karachi restaurants dhoondo', 'leads banao', 'demo live karo'."
                % (state.get("actions_today", 0), state.get("leads_found", 0),
                   len(state.get("pages_published", []))))

    @staticmethod
    def _parse(lowered: str) -> tuple:
        import re as _re
        match = _re.search(r"(\d{1,3})\s*(?:leads?|repos?)", lowered)
        city, niche = "Karachi", "restaurant"
        for candidate in ("Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad",
                          "Multan", "Peshawar"):
            if candidate.lower() in lowered:
                city = candidate
                break
        for candidate in ("restaurant", "clinic", "salon", "gym", "pharmacy", "boutique",
                          "hotel", "real estate", "school", "electronics", "software"):
            if candidate in lowered:
                niche = candidate
                break
        return city, niche
