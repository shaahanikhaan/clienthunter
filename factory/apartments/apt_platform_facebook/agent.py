"""apt_platform_facebook - Facebook Page outreach (Groups + Messenger).

Realistic scope, stated honestly:

*   **Groups** work.  ``facebook.com/groups/<slug>/`` is public for most
    Karachi/Lahore business groups; posting there only needs the user's logged-in
    Chrome session (CDP), no API.
*   **Pages/Groups discovery** via ``google.com/search`` + ``facebook.com``
    filters, driven by CDP.
*   **Messenger** (``facebook.com/messages/t/<id>``) is GUI-only - it is routed
    through the mouse tool and always needs approval.
*   **No Graph API** - that would mean an app id + token, which the spec forbids.

The agent is deliberately conservative: Facebook is the harshest platform about
automation, so the daily cap is 15, actions are spaced by a random 60-180 s
pause, and outreach only fires inside Pakistan working hours.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

PAKISTAN = timezone(timedelta(hours=5))
DAILY_CAP = 15
WORK_START, WORK_END = 9, 19
MAX_WORDS = 90

SEARCH_QUERIES = (
    'site:facebook.com/groups "{city}" "{niche}"',
    '"{city}" {niche} business group facebook',
    'facebook group "{city}" business owners {niche}',
)


class PlatformFacebookAgent(BaseApartment):
    NAME = "platform_facebook"
    DESCRIPTION = "Facebook Groups + Messenger outreach through the real Chrome session"
    PERMISSIONS = {"browser": True, "internet": True, "whatsapp": False,
                   "mouse": False, "email": False}

    PLAYBOOK = "platform_posts.json"
    STATE = "facebook_state.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        playbook = self._playbook()
        facebook = playbook.get("facebook") or playbook.get("platforms", {}).get("facebook", {})
        model = {
            "platform": "facebook",
            "daily_cap": DAILY_CAP,
            "work_window": [WORK_START, WORK_END],
            "max_words": MAX_WORDS,
            "search_queries": SEARCH_QUERIES,
            "post_templates": facebook.get("posts", []) or facebook.get("templates", []),
            "best_time": facebook.get("best_time", "11:00-13:00"),
            "tone": facebook.get("tone", "community member, never a sales pitch"),
            "rules": [
                "Groups: contribute first - answer a question before you post anything.",
                "Never post a link on first contact; groups delete that instantly.",
                "No copy-paste across groups; rewrite for each community.",
                "Messenger only after the person engaged publicly.",
            ],
            "trained_from": "data/platform_posts.json",
        }
        self.save_model("facebook_playbook.json", model)
        self.save_model(self.STATE, {"sent_today": 0, "day": self._today(),
                                     "total_sent": 0, "groups_seen": [],
                                     "engaged": [], "replies": 0, "last_run": 0.0})
        self.log("trained", templates=len(model["post_templates"]))
        return {"templates": len(model["post_templates"]), "cap": DAILY_CAP}

    def _playbook(self) -> Dict[str, Any]:
        return self.load_shared_data(self.PLAYBOOK, default={}) or {}

    def _state(self) -> Dict[str, Any]:
        state = self.load_model(self.STATE, default={}) or {}
        if state.get("day") != self._today():
            state["sent_today"] = 0
            state["day"] = self._today()
        return state

    @staticmethod
    def _today() -> str:
        return datetime.now(PAKISTAN).strftime("%Y-%m-%d")

    @staticmethod
    def in_window() -> bool:
        return WORK_START <= datetime.now(PAKISTAN).hour < WORK_END

    def _save(self, state: Dict[str, Any]) -> None:
        state["last_run"] = time.time()
        self.save_model(self.STATE, state)

    # ------------------------------------------------------------ discovery
    def find_groups(self, city: str = "Karachi", niche: str = "restaurant",
                    limit: int = 8) -> List[Dict[str, Any]]:
        """Find public groups via web search (robots-respecting, 1 req / 2 s)."""
        web = self.tool("web")
        groups: List[Dict[str, Any]] = []
        for template in SEARCH_QUERIES:
            query = template.format(city=city, niche=niche)
            results = []
            try:
                results = web.search(query, limit=limit) or []
            except Exception:
                results = []
            for result in results:
                url = result.get("url", "")
                if "facebook.com/groups" not in url:
                    continue
                groups.append({"platform": "facebook", "url": url,
                               "title": result.get("title", "")[:120],
                               "city": city, "niche": niche,
                               "source": "search"})
                if len(groups) >= limit:
                    break
            time.sleep(2.0)          # 1 request / 2 s floor
            if len(groups) >= limit:
                break
        self.log("groups_found", city=city, niche=niche, count=len(groups))
        return groups[:limit]

    # ------------------------------------------------------------- composing
    def compose_post(self, lead: Dict[str, Any], audit: Optional[Dict[str, Any]] = None) -> str:
        issues = (audit or {}).get("issues") or []
        top = issues[0] if issues else None
        name = (lead.get("business_name") or "your business").strip()
        city = lead.get("city") or "Karachi"
        if top:
            body = (
                "Salam everyone - I audit {niche} websites in {city} as a side project.\n\n"
                "Looked at {name} recently: {issue}\n"
                "{impact}\n\n"
                "If anyone wants the same check for their own site, comment or DM - free, "
                "no strings, I just send the report."
            ).format(
                niche=(lead.get("category") or "local").replace("_", " "),
                city=city, name=name,
                issue=top.get("headline", "their site has a fixable issue"),
                impact=top.get("impact", "It is costing them bookings every week."),
            )
        else:
            body = (
                "Salam everyone - I build fast, mobile-first websites for {city} "
                "businesses.\n\nIf anyone's site takes more than 3 seconds on mobile, "
                "comment and I'll send a free one-page audit - {niche} especially, "
                "where most bookings come from phones."
            ).format(city=city, niche=(lead.get("category") or "local").replace("_", " "))
        words = body.split()
        return " ".join(words[:MAX_WORDS])

    def compose_dm(self, lead: Dict[str, Any], audit: Optional[Dict[str, Any]] = None) -> str:
        """Messenger opener - only for people who engaged publicly first."""
        issues = (audit or {}).get("issues") or []
        top = issues[0] if issues else None
        if not top:
            return ""
        return ("Salam {name}! Aapki {niche} site dekhi thi - {issue}. "
                "Is se har hafte kuch bookings miss ho rahi hain. "
                "Free 1-page report bana kar bhej doon? Koi charge nahi."
                ).format(name=(lead.get("business_name") or "sir").split()[0],
                         niche=(lead.get("category") or "business").replace("_", " "),
                         issue=top.get("headline", "mobile speed slow hai"))

    # ---------------------------------------------------------------- sending
    def send_post(self, group_url: str, text: str) -> Dict[str, Any]:
        state = self._state()
        if state["sent_today"] >= DAILY_CAP:
            return {"ok": False, "reason": "daily cap reached (%d)" % DAILY_CAP}
        if not self.in_window():
            return {"ok": False, "reason": "outside Pakistan working hours (9am-7pm)"}
        browser = self.tool("browser")
        try:
            if self.offline or not browser.is_running():
                return {"ok": False, "reason":
                        "chrome not reachable - open Chrome with --remote-debugging-port=9222"}
            session = browser.visit(group_url, wait_s=4.0)
            if session is None:
                return {"ok": False, "reason": "could not open the group page"}
            typed = False
            for selector in ('div[role="textbox"]',
                             'div[aria-label*="Write something"]',
                             'div[aria-label*="Create a public post"]',
                             'textarea'):
                if session.wait_for(selector, timeout_s=6.0):
                    typed = session.type_text(selector, text)
                    if typed:
                        break
            if not typed:
                return {"ok": False,
                        "reason": "composer not found (group may require membership)"}
            self._bump(state, "post", group_url)
            return {"ok": True, "url": group_url, "chars": len(text)}
        except Exception as exc:
            return {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}

    def _bump(self, state: Dict[str, Any], action: str, target: str) -> None:
        state["sent_today"] = int(state.get("sent_today", 0)) + 1
        state["total_sent"] = int(state.get("total_sent", 0)) + 1
        state.setdefault("groups_seen", []).append({"target": target[:60], "action": action,
                                                    "at": self._today()})
        state["groups_seen"] = state["groups_seen"][-200:]
        self._save(state)

    def run(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Daily execution: audit-backed posts into relevant groups."""
        if not self.in_window():
            self.log("skipped", reason="outside working hours")
            return []
        store = default_store()
        leads = store.leads(stage="audited", limit=limit)
        if not leads:
            leads = store.leads(stage="enriched", limit=limit)
        if not leads:
            leads = store.leads(limit=limit)
        results = []
        for lead in leads:
            state = self._state()
            if state["sent_today"] >= DAILY_CAP:
                break
            audit = self.recall("audit:%s" % lead.get("id")) or {}
            text = self.compose_post(lead, audit)
            groups = self.find_groups(lead.get("city") or "Karachi",
                                      lead.get("category") or "business", limit=1)
            target = groups[0]["url"] if groups else ""
            if not target:
                results.append({"lead_id": lead.get("id"), "ok": False,
                                "reason": "no public group found"})
                continue
            outcome = self.send_post(target, text)
            outcome.update({"lead_id": lead.get("id"), "text": text})
            results.append(outcome)
            if outcome.get("ok"):
                store.log_message(int(lead["id"]), "out", "facebook", text[:300])
                store.set_stage(int(lead["id"]), "contacted")
                store.touch(int(lead["id"]))
            time.sleep(random.uniform(60, 180))     # look human, avoid rate limits
        self.log("run", results=len(results), sent=sum(1 for r in results if r.get("ok")))
        return results

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        if any(word in lowered for word in ("group", "groups", "dhoondo", "find")):
            city = "Karachi"
            for candidate in ("Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad"):
                if candidate.lower() in lowered:
                    city = candidate
                    break
            niche = "business"
            for candidate in ("restaurant", "clinic", "salon", "gym", "pharmacy",
                              "boutique", "hotel", "real estate", "school"):
                if candidate in lowered:
                    niche = candidate
                    break
            groups = self.find_groups(city, niche, limit=8)
            if not groups:
                return ("Koi public group nahi mila '%s' ke liye. Chrome khula hona chahiye "
                        "(remote debugging port 9222)." % city)
            return "\n".join("%s\n  %s" % (group["title"][:90], group["url"])
                             for group in groups)
        if any(word in lowered for word in ("post", "bhejo", "send", "likho")):
            store = default_store()
            leads = store.leads(stage="audited", limit=1) or store.leads(limit=1)
            if not leads:
                return "Pehle leads hunt karein."
            audit = self.recall("audit:%s" % leads[0].get("id")) or {}
            return self.compose_post(leads[0], audit)
        state = self._state()
        return ("Facebook agent ready. Aaj %d/%d posts bheje. Total %d.\nWindow 9am-7pm PKT. "
                "Kaam: 'groups dhoondo', 'post banao', 'send'." % (
                    state.get("sent_today", 0), DAILY_CAP, state.get("total_sent", 0)))

    def status(self) -> Dict[str, Any]:
        data = super().status()
        state = self._state()
        data.update({"sent_today": state.get("sent_today", 0), "cap": DAILY_CAP,
                     "total_sent": state.get("total_sent", 0),
                     "in_window": self.in_window()})
        return data
