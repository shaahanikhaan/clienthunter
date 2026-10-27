"""apt_platform_instagram - Instagram outreach: profile audit -> comment -> DM.

Instagram is the strictest platform in the set, so this agent is the most
conservative:

*   **15 actions/day**, taken only inside the hours the playbook records as
    responsive (12-14 and 20-22 PKT).
*   **Two-step.**  A genuine public comment first (proof you looked), a DM only
    afterwards.  Cold DMs from a fresh account get the account flagged; a
    comment on a real post does not.
*   **60 words max.**  People decide in three seconds.
*   **No link in the first message** - Instagram collapses DMs containing URLs,
    so the link waits until they reply.

Discovery is public: DuckDuckGo for ``site:instagram.com "<city>" <niche>``,
then the profile's page (through the user's logged-in Chrome via CDP) to read
the bio link - which, if it is missing, broken, or a linktree to a dead site,
is exactly the kind of specific finding the opener needs.

No Graph API, no tokens, no third-party automation service.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

PAKISTAN = timezone(timedelta(hours=5))
STATE = "instagram_state.json"
PLAYBOOK = "platform_posts.json"

#: Used only when data/platform_posts.json has not been trained in yet.
DEFAULT_CAP = 15
DEFAULT_MAX_WORDS = 60
BEST_HOURS = (12, 13, 14, 20, 21, 22)

SEARCH_QUERIES = (
    'site:instagram.com "{city}" {niche}',
    '"{city}" {niche} instagram page',
    'instagram "{city}" {niche} official',
)


class PlatformInstagramAgent(BaseApartment):
    NAME = "platform_instagram"
    DESCRIPTION = "Instagram outreach: profile audit -> real comment -> short DM"
    PERMISSIONS = {"browser": True, "internet": True, "whatsapp": False,
                   "mouse": False, "email": False}

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        playbook = self._playbook()
        instagram = playbook.get("instagram") or {}
        model = {
            "platform": "instagram",
            "daily_cap": int(instagram.get("daily_limit", DEFAULT_CAP)),
            "max_words": int(instagram.get("max_words", DEFAULT_MAX_WORDS)),
            "best_hours": instagram.get("best_hours", list(BEST_HOURS)),
            "best_days": instagram.get("best_days", []),
            "avoid_hours": instagram.get("avoid_hours", []),
            "style": instagram.get("style", "bohat short, emoji kam, pehle genuine compliment"),
            "opener_pattern": instagram.get("opener_pattern",
                                            "{compliment} ... {issue} ... {question}"),
            "templates": instagram.get("templates", []),
            "rules": [
                "Pehle genuine comment (public proof), DM baad mein.",
                "Pehle message mein link mat bhejo - DM collapse ho jata hai.",
                "Har account ke liye alfaaz badlo - copy-paste detect ho jata hai.",
                "Business hours ke bahar kuch mat bhejo.",
            ],
            "trained_from": "data/platform_posts.json",
        }
        self.save_model("instagram_playbook.json", model)
        self.save_model(STATE, {"actions_today": 0, "day": self._today(),
                                "total_sent": 0, "profiles_seen": [],
                                "replies": 0, "last_run": 0.0})
        self.log("trained", templates=len(model["templates"]))
        return {"templates": len(model["templates"]), "cap": model["daily_cap"]}

    def _playbook(self) -> Dict[str, Any]:
        data = self.load_shared_data(PLAYBOOK, default={}) or {}
        if isinstance(data, dict) and isinstance(data.get("platforms"), dict):
            return data["platforms"]
        return data if isinstance(data, dict) else {}

    def _model(self) -> Dict[str, Any]:
        return self.load_model("instagram_playbook.json", default={}) or {}

    def _state(self) -> Dict[str, Any]:
        state = self.load_model(STATE, default={}) or {}
        if state.get("day") != self._today():
            state["actions_today"] = 0
            state["day"] = self._today()
        return state

    @staticmethod
    def _today() -> str:
        return datetime.now(PAKISTAN).strftime("%Y-%m-%d")

    def cap(self) -> int:
        return int(self._model().get("daily_cap", DEFAULT_CAP))

    def max_words(self) -> int:
        return int(self._model().get("max_words", DEFAULT_MAX_WORDS))

    def in_window(self) -> bool:
        hours = self._model().get("best_hours") or list(BEST_HOURS)
        return datetime.now(PAKISTAN).hour in [int(h) for h in hours]

    def _save(self, state: Dict[str, Any]) -> None:
        state["last_run"] = time.time()
        self.save_model(STATE, state)

    # ------------------------------------------------------------- discovery
    def find_profiles(self, city: str = "Karachi", niche: str = "boutique",
                      limit: int = 8) -> List[Dict[str, Any]]:
        web = self.tool("web")
        found: List[Dict[str, Any]] = []
        seen = set()
        for template in SEARCH_QUERIES:
            if len(found) >= limit:
                break
            query = template.format(city=city, niche=niche)
            try:
                results = web.search(query, limit=limit) or []
            except Exception:
                results = []
            for result in results:
                url = (result.get("url") or "").split("?")[0]
                if "instagram.com/" not in url:
                    continue
                handle = url.rstrip("/").split("/")[-1]
                if not handle or handle in seen or handle in ("p", "reel", "explore"):
                    continue
                seen.add(handle)
                found.append({"platform": "instagram", "handle": handle, "url": url,
                              "title": (result.get("title") or "")[:120],
                              "city": city, "category": niche})
            time.sleep(2.0)          # 1 request / 2 s floor
        self.log("profiles_found", city=city, niche=niche, count=len(found))
        return found[:limit]

    def read_profile(self, handle: str) -> Dict[str, Any]:
        """Open the profile in the user's Chrome and read what is publicly there.

        Returns real fields when Chrome is reachable, and an honest
        ``unavailable`` marker when it is not - never fabricated numbers.
        """
        browser = self.tool("browser")
        if self.offline or not browser.is_running():
            return {"handle": handle, "unavailable": True,
                    "reason": "chrome not on port 9222"}
        try:
            session = browser.visit("https://www.instagram.com/%s/" % handle, wait_s=4.0)
            if session is None:
                return {"handle": handle, "unavailable": True,
                        "reason": "could not open profile"}
            text = session.extract_text("body", limit=8000)
        except Exception as exc:
            return {"handle": handle, "unavailable": True,
                    "reason": "%s: %s" % (type(exc).__name__, exc)[:120]}
        numbers = re.findall(r"([\d.,KMB]+)\s+(posts|followers|following)", text, re.I)
        stats = {kind: value for value, kind in numbers[:3]}
        link = ""
        match = re.search(r"https?://(?!www\.instagram\.com|instagram\.com)[^\s\"'<>]+", text)
        if match:
            link = match.group(0)
        return {"handle": handle, "stats": stats, "bio_link": link,
                "has_highlights": "highlights" in text.lower(),
                "text_sample": text[:400]}

    #: Findings we can prove from a public profile alone.
    def findings_for(self, profile: Dict[str, Any]) -> List[str]:
        issues: List[str] = []
        if profile.get("unavailable"):
            return issues
        if not profile.get("bio_link"):
            issues.append("bio mein koi website link nahi")
        stats = profile.get("stats") or {}
        posts = str(stats.get("posts", ""))
        if posts and posts[-1] in "KMB" and len(posts) > 3:
            issues.append("active account hai par website link nahi")
        if not profile.get("has_highlights"):
            issues.append("no highlights - menu/timings save nahi hue")
        return issues

    # -------------------------------------------------------------- composing
    def compose_comment(self, lead: Dict[str, Any],
                        profile: Optional[Dict[str, Any]] = None) -> str:
        """Public comment - specific, human, never a pitch."""
        category = (lead.get("category") or "business").replace("_", " ")
        name = (lead.get("business_name") or lead.get("name") or "aap ka").strip()
        issues = self.findings_for(profile or {})
        if issues:
            return ("Bohat achi {category} content! Sirf ek cheez: {issue}. "
                    "Chhota fix hai, 2 minute mein ho jata hai."
                    ).format(category=category, issue=issues[0])
        return ("{name} ki {category} posts bohat consistent hain - respect. "
                "Agar kabhi website ki taraf bhi dhyan dena ho to bata dijiyega, "
                "main free audit kar deta hoon.").format(name=name, category=category)

    def compose_dm(self, lead: Dict[str, Any],
                   profile: Optional[Dict[str, Any]] = None) -> str:
        issues = self.findings_for(profile or {})
        name = (lead.get("business_name") or lead.get("name") or "sir").strip()
        category = (lead.get("category") or "business").replace("_", " ")
        if issues:
            return ("Salam! {name} ka page dekha - {category} content bohat acha hai. "
                    "Ek cheez notice hui: {issue}. Is se bookings miss ho rahi hain. "
                    "Free 1-page mockup bana doon? Bilkul free.").format(
                name=name, category=category, issue=issues[0])
        return ("Salam! {name} ka {category} page dekha, bohat acha chal raha hai. "
                "Agar mobile site bhi utni fast ho jaye to aur bookings aayengi. "
                "Free audit bhejoon?").format(name=name, category=category)

    # ---------------------------------------------------------------- sending
    def send_dm(self, handle: str, text: str) -> Dict[str, Any]:
        state = self._state()
        if state.get("actions_today", 0) >= self.cap():
            return {"ok": False, "reason": "daily cap reached (%d)" % self.cap()}
        if not self.in_window():
            return {"ok": False, "reason": "outside the responsive window (12-14, 20-22 PKT)"}
        browser = self.tool("browser")
        if self.offline or not browser.is_running():
            return {"ok": False,
                    "reason": "chrome not reachable (--remote-debugging-port=9222)"}
        try:
            session = browser.visit("https://www.instagram.com/direct/new/", wait_s=4.0)
            if session is None:
                return {"ok": False, "reason": "instagram DM page did not open"}
            if not session.wait_for('input[name="queryBox"], input[placeholder*="Search"]',
                                    timeout_s=8.0):
                return {"ok": False, "reason": "DM search box not found (UI changed)"}
            session.type_text('input[name="queryBox"], input[placeholder*="Search"]', handle)
            time.sleep(2.0)
            session.press("Enter")
            time.sleep(1.5)
            typed = False
            for selector in ('div[role="textbox"]', 'textarea[placeholder*="Message"]',
                             'div[aria-label*="Message"]'):
                if session.wait_for(selector, timeout_s=6.0):
                    typed = session.type_text(selector, text)
                    if typed:
                        break
            if not typed:
                return {"ok": False, "reason": "message box not found"}
            state["actions_today"] = int(state.get("actions_today", 0)) + 1
            state["total_sent"] = int(state.get("total_sent", 0)) + 1
            state.setdefault("profiles_seen", []).append(
                {"handle": handle, "at": self._today()})
            state["profiles_seen"] = state["profiles_seen"][-200:]
            self._save(state)
            return {"ok": True, "handle": handle, "chars": len(text)}
        except Exception as exc:
            return {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}

    def comment_on_recent(self, handle: str, text: str) -> Dict[str, Any]:
        """Leave the public comment that earns the right to send a DM."""
        browser = self.tool("browser")
        if self.offline or not browser.is_running():
            return {"ok": False, "reason": "chrome not reachable"}
        try:
            session = browser.visit("https://www.instagram.com/%s/" % handle, wait_s=4.0)
            if session is None:
                return {"ok": False, "reason": "profile did not open"}
            clicked = session.click('article a, div[class*="_aagw"]')
            if not clicked:
                return {"ok": False, "reason": "no recent post to comment on"}
            time.sleep(2.5)
            typed = False
            for selector in ('textarea[placeholder*="comment" i]',
                             'form textarea', 'div[role="textbox"]'):
                if session.wait_for(selector, timeout_s=6.0):
                    typed = session.type_text(selector, text)
                    if typed:
                        break
            if not typed:
                return {"ok": False, "reason": "comment box not found"}
            session.press("Enter")
            return {"ok": True, "handle": handle}
        except Exception as exc:
            return {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}

    # -------------------------------------------------------------------- run
    def run(self, limit: int = 5) -> List[Dict[str, Any]]:
        if not self.in_window():
            self.log("skipped", reason="outside responsive hours")
            return []
        store = default_store()
        leads = store.leads(stage="audited", limit=limit) or \
            store.leads(stage="contacted", limit=limit) or store.leads(limit=limit)
        results: List[Dict[str, Any]] = []
        for lead in leads:
            if self._state().get("actions_today", 0) >= self.cap():
                break
            handle = self._handle_for(lead)
            if not handle:
                results.append({"lead_id": lead.get("id"), "ok": False,
                                "reason": "no instagram handle found"})
                continue
            profile = self.read_profile(handle)
            comment = self.comment_on_recent(handle, self.compose_comment(lead, profile))
            outcome = {"lead_id": lead.get("id"), "handle": handle,
                       "comment": comment.get("ok", False),
                       "findings": self.findings_for(profile)}
            if comment.get("ok"):
                time.sleep(random.uniform(45, 120))
                dm = self.send_dm(handle, self.compose_dm(lead, profile))
                outcome["dm"] = dm.get("ok", False)
                outcome["reason"] = dm.get("reason", "")
                if dm.get("ok"):
                    store.log_message(int(lead["id"]), "out", "instagram",
                                      self.compose_dm(lead, profile)[:300])
                    store.touch(int(lead["id"]))
            else:
                outcome["reason"] = comment.get("reason", "")
            results.append(outcome)
            time.sleep(random.uniform(90, 240))     # Instagram is strict - go slow
        self.log("run", results=len(results),
                 sent=sum(1 for r in results if r.get("dm")))
        return results

    @staticmethod
    def _handle_for(lead: Dict[str, Any]) -> str:
        socials = lead.get("socials")
        if isinstance(socials, dict):
            for key, value in socials.items():
                if "instagram" in str(key).lower() and value:
                    handle = str(value).rstrip("/").split("/")[-1].lstrip("@")
                    return handle
        if isinstance(socials, list):
            for value in socials:
                if "instagram.com" in str(value):
                    return str(value).rstrip("/").split("/")[-1].lstrip("@")
        notes = str(lead.get("notes") or "")
        match = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", notes)
        return match.group(1).lstrip("@") if match else ""

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        if any(word in lowered for word in ("dhoondo", "find", "search", "profiles")):
            city, niche = self._parse(lowered)
            profiles = self.find_profiles(city, niche, limit=8)
            if not profiles:
                return "Koi Instagram page nahi mila '%s %s' ke liye." % (city, niche)
            return "\n".join("  @%-24s %s" % (p["handle"], p["url"]) for p in profiles)
        if any(word in lowered for word in ("check", "profile", "audit")):
            handle = self._handle_in(text)
            if not handle:
                return "Handle do, jaise: '@aliboutique check karo'."
            profile = self.read_profile(handle)
            if profile.get("unavailable"):
                return "@%s check nahi ho saka: %s" % (handle, profile.get("reason"))
            issues = self.findings_for(profile)
            return ("@%s -> stats %s, bio link: %s\nFindings: %s"
                    % (handle, profile.get("stats") or "-",
                       profile.get("bio_link") or "none",
                       "; ".join(issues) or "koi masla nahi mila"))
        if any(word in lowered for word in ("dm", "message", "bhejo")):
            store = default_store()
            lead = (store.leads(stage="audited", limit=1) or store.leads(limit=1) or [{}])[0]
            handle = self._handle_in(text) or self._handle_for(lead)
            if not handle:
                return "Koi handle nahi mila. Pehle leads enrich karein."
            profile = self.read_profile(handle)
            return self.compose_dm(lead, profile)
        state = self._state()
        return ("Instagram agent ready. Aaj %d/%d actions. Total %d.\n"
                "Window: %s PKT. Kaam: 'profiles dhoondo', '@handle check karo', 'dm bhejo'."
                % (state.get("actions_today", 0), self.cap(),
                   state.get("total_sent", 0),
                   ", ".join(str(h) for h in (self._model().get("best_hours") or BEST_HOURS))))

    @staticmethod
    def _parse(lowered: str) -> tuple:
        city, niche = "Karachi", "boutique"
        for candidate in ("Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad",
                          "Multan", "Peshawar"):
            if candidate.lower() in lowered:
                city = candidate
                break
        for candidate in ("boutique", "restaurant", "cafe", "salon", "clinic", "gym",
                          "photographer", "bakery", "real estate", "hotel"):
            if candidate in lowered:
                niche = candidate
                break
        return city, niche

    @staticmethod
    def _handle_in(text: str) -> str:
        match = re.search(r"@([A-Za-z0-9_.]{2,30})", text or "")
        return match.group(1) if match else ""

    def status(self) -> Dict[str, Any]:
        data = super().status()
        state = self._state()
        data.update({"actions_today": state.get("actions_today", 0), "cap": self.cap(),
                     "total_sent": state.get("total_sent", 0),
                     "in_window": self.in_window()})
        return data
