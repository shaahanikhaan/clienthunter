"""apt_followup - the persistence engine.

Most deals are lost to silence, not to "no".  This apartment runs 6 touches over
23 days, and the rule is absolute: **each follow-up must carry new value**.  A
"just checking in" message burns the relationship, so :meth:`compose` refuses to
produce one - every touch draws from a different value source:

    1. a second (different) audit finding
    2. a free mockup offer
    3. a case study from the same category
    4. the monthly option (removes the budget wall)
    5. a seasonal / market insight
    6. a polite last call (which, surprisingly, converts)

Timing is learned: ``models/timing.json`` tracks which day/hour produced replies,
and apt_self_trainer rewrites the schedule from your real data.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

#: Value source per touch number.  Never repeat within a sequence.
VALUE_ANGLES = [
    {"touch": 1, "angle": "second_audit_finding",
     "hint": "Ek aur masla mila jo pichli baat mein mention nahi hua tha"},
    {"touch": 2, "angle": "free_mockup", "hint": "Free mockup - dekh kar decide karein"},
    {"touch": 3, "angle": "case_study", "hint": "Aap jaisay ek business ka result"},
    {"touch": 4, "angle": "monthly_option", "hint": "Budget ka rasta - monthly plan"},
    {"touch": 5, "angle": "market_insight",
     "hint": "Aap ke shehar / category ka ek insight"},
    {"touch": 6, "angle": "last_call", "hint": "Aakhri baat - door khula hai"},
]


class FollowupAgent(BaseApartment):
    NAME = "followup"
    DESCRIPTION = "5+ value-carrying follow-ups per lead; never 'just checking in'"
    PERMISSIONS = {"email": True, "whatsapp": True, "internet": True}

    MODEL = "timing.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        agency = self.agency() or {}
        rules = agency.get("outreach_rules", {}) or {}
        schedule = rules.get("followup_schedule_days") or [2, 4, 7, 11, 16, 23]
        platform = (self.load_shared_data("platform_posts.json", default={}) or {}
                    ).get("platforms", {}).get("whatsapp", {}) or {}

        learned = self.load_model(self.MODEL, default={}) or {}
        model = {
            "schedule_days": learned.get("schedule_days") or [int(d) for d in schedule],
            "best_hours": learned.get("best_hours") or platform.get("best_hours",
                                                                    [10, 11, 17, 18, 19]),
            "best_days": learned.get("best_days") or platform.get("best_days",
                                                                  ["monday", "tuesday",
                                                                   "wednesday", "thursday"]),
            "angle_stats": learned.get("angle_stats") or
            {angle["angle"]: {"sent": 0, "replied": 0} for angle in VALUE_ANGLES},
            "max_touches": int(self.config.get("limits", {}).get("max_touches", 6)),
            "notes": "Day/hour/angle performance. apt_self_trainer rewrites this from "
                     "real reply timestamps.",
        }
        self.save_model(self.MODEL, model)
        self.log("trained", schedule=model["schedule_days"])
        return {"schedule": model["schedule_days"], "angles": len(model["angle_stats"])}

    def timing(self) -> Dict[str, Any]:
        return self.load_model(self.MODEL, default={}) or {"schedule_days": [2, 4, 7, 11, 16, 23]}

    # -------------------------------------------------------------- scheduling
    def schedule_sequence(self, lead_id: int) -> List[int]:
        """Queue the whole sequence for a lead (idempotent)."""
        store = default_store()
        existing = store.pending_followups(int(lead_id))
        if existing:
            return [item["id"] for item in existing]
        timing = self.timing()
        days = timing.get("schedule_days") or [2, 4, 7, 11, 16, 23]
        hour = (timing.get("best_hours") or [11])[0]
        ids = []
        for index, day in enumerate(days[:int(timing.get("max_touches", 6))]):
            angle = VALUE_ANGLES[index % len(VALUE_ANGLES)]
            due = self._next_due(day, hour, lead_id)
            ids.append(store.schedule_followup(int(lead_id), due, index + 1,
                                                angle["angle"], "", "whatsapp"))
        self.log("sequence_scheduled", lead_id=lead_id, touches=len(ids))
        return ids

    def _next_due(self, days: int, hour: int, lead_id: Any = None) -> float:
        """Due date = the day; the *hour* comes from the send-time scheduler.

        Everything is computed in Karachi time (``factory.scheduler``), so a
        follow-up lands in a real reply window instead of 11:00 on whatever
        clock the machine happens to keep.
        """
        try:
            from datetime import timedelta
            from ...scheduler import best_time, now
            return best_time("whatsapp", lead_id=lead_id,
                             moment=now() + timedelta(days=int(days)))
        except Exception:
            target = time.time() + days * 86400
            local = time.localtime(target)
            return time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                int(hour), 0, 0, local.tm_wday, local.tm_yday,
                                local.tm_isdst))

    def schedule_followup(self, lead_id: int, days: int = 3,
                          angle: str = "second_audit_finding") -> int:
        store = default_store()
        timing = self.timing()
        hour = (timing.get("best_hours") or [11])[0]
        return store.schedule_followup(int(lead_id),
                                       self._next_due(days, hour, lead_id),
                                       1, angle, "", "whatsapp")

    # ---------------------------------------------------------------- compose
    def compose(self, lead: Dict[str, Any], touch: int = 1,
                angle: Optional[str] = None) -> Dict[str, str]:
        """Build one follow-up.  Guaranteed to contain a new value payload."""
        angle = angle or VALUE_ANGLES[min(touch, len(VALUE_ANGLES)) - 1]["angle"]
        builder = getattr(self, "_angle_%s" % angle, self._angle_second_audit_finding)
        body = builder(lead, touch)
        body = re.sub(r"\s+", " ", body).strip()
        if len(body.split()) > 100:
            body = " ".join(body.split()[:100]) + "..."
        # Hard guard: refuse to send anything that is just a nudge.
        if _is_nudge(body):
            body = self._angle_free_mockup(lead, touch)
        return {"angle": angle, "body": body, "touch": touch}

    def _issues(self, lead: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        return issues or []

    def _angle_second_audit_finding(self, lead: Dict[str, Any], touch: int) -> str:
        issues = self._issues(lead)
        if len(issues) > 1:
            issue = issues[1]
            return ("Salam, ek aur cheez mili jo pehle batai thi us se alag hai: %s. "
                    "Is ki wajah se %s. Aap chahein to apne developer ko ye line de dein - "
                    "ho sakta hai wo 10 minute mein theek kar de."
                    % (issue.get("title", ""), issue.get("benefit", "")))
        return ("Salam, main ne dobara check kiya to %s ki site ka score abhi bhi %s/100 hai. "
                "Ek chhota sa test: apne phone se site khol kar dekhein - kitne second lagte hain?"
                % (lead.get("name", "aap ki site"), lead.get("audit_score", 0)))

    def _angle_free_mockup(self, lead: Dict[str, Any], touch: int) -> str:
        return ("Salam, main ne %s ka free homepage mockup bana liya hai - aap ke asli naam, "
                "aur WhatsApp button ke sath. Bhejoon? Pasand na aaye to koi paisa nahi, "
                "koi commitment nahi. Sirf haan ya na batayein."
                % lead.get("name", "aap ka business"))

    def _angle_case_study(self, lead: Dict[str, Any], touch: int) -> str:
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == lead.get("category")), {})
        agency = self.agency() or {}
        items = agency.get("portfolio", []) or []
        item = next((i for i in items if i.get("category") == lead.get("category")),
                    items[0] if items else {})
        return ("Salam, aap jaisay ek %s ka example: %s (%s) - %s. "
                "Wahi approach %s ke liye bhi kaam kar sakti hai. Ek mockup bana doon?"
                % (category.get("name", "business"), item.get("name", "ek client"),
                   item.get("city", ""), item.get("result", "result acha mila"),
                   lead.get("name", "aap ke business")))

    def _angle_monthly_option(self, lead: Dict[str, Any], touch: int) -> str:
        packages = (self.agency() or {}).get("packages", {}) or {}
        basic = packages.get("basic", {})
        return ("Salam, soch raha hoon shayad upfront amount rukawat hai. "
                "Monthly option hai: sirf %s PKR mahine (basic package) - hosting shamil, "
                "jab chahein rok dein. Is tarah shuruat karna asaan hota hai. "
                "Kya ye zyada munasib rahega?" % _fmt(basic.get("monthly", 1500)))

    def _angle_market_insight(self, lead: Dict[str, Any], touch: int) -> str:
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == lead.get("category")), {})
        pain = (category.get("pain_points") or ["customers online nahi milte"])[0]
        return ("Salam, aap ke shehar (%s) mein %s ke liye ek baat dekhi: %s. "
                "Jo business is ko pehle solve karta hai, usay faeda milta hai. "
                "%s ke liye 2 minute baat kar lein?"
                % (lead.get("city", "aap ke shehar"), lead.get("category", "business"),
                   pain, lead.get("name", "Aap")))

    def _angle_last_call(self, lead: Dict[str, Any], touch: int) -> str:
        return ("Salam, main aakhri baar message kar raha hoon - baar baar pareshan karna "
                "theek nahi. %s ki site ka audit report aap ke paas hai, wo free hai aur "
                "kaam aayega chahe aap hum se kaam karein ya nahi. "
                "Agar kabhi dil kare to bas ek message kar dein." % lead.get("name", "Aap ki site"))

    # ------------------------------------------------------------------- send
    def send_due(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Send every follow-up whose time has come."""
        store = default_store()
        due = store.due_followups()
        results = []
        for item in due[:limit]:
            if self.kill_switch():
                break
            lead = store.get_lead(int(item["lead_id"]))
            if not lead or lead.get("stage") in ("won", "lost"):
                store.complete_followup(int(item["id"]), "skipped")
                continue
            message = self.compose(lead, int(item.get("touch") or 1), item.get("angle"))
            channel = item.get("channel") or "whatsapp"
            result: Dict[str, Any] = {"ok": False}
            if channel == "whatsapp" and lead.get("phone"):
                result = self.tool("whatsapp").send_message(lead["phone"], message["body"])
            elif lead.get("email"):
                result = self.tool("email").send(
                    lead["email"], "Re: %s" % lead.get("name", "aap ki site"),
                    message["body"])
            else:
                result = {"ok": False, "reason": "no channel available"}
            if result.get("ok"):
                store.complete_followup(int(item["id"]), "sent")
                store.touch(int(lead["id"]))
                store.log_message(int(lead["id"]), "out", channel, message["body"],
                                  intent="followup")
                self._record_angle(message["angle"], sent=True)
            else:
                store.complete_followup(int(item["id"]), "failed")
            results.append({"lead": lead.get("name"), "touch": item.get("touch"),
                            "angle": message["angle"], "sent": bool(result.get("ok")),
                            "reason": result.get("reason", "")})
        if results:
            self.remember("last_run", {"at": time.time(), "sent":
                                       sum(1 for r in results if r["sent"])})
        return results

    def _record_angle(self, angle: str, sent: bool = True, replied: bool = False) -> None:
        model = self.timing()
        stats = model.setdefault("angle_stats", {})
        entry = stats.setdefault(angle, {"sent": 0, "replied": 0})
        entry["sent"] += 1 if sent else 0
        entry["replied"] += 1 if replied else 0
        self.save_model(self.MODEL, model)

    def record_reply(self, angle: str) -> None:
        self._record_angle(angle, sent=False, replied=True)

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").lower()
        store = default_store()

        if any(word in text for word in ("dikhao", "preview", "draft", "likho")):
            leads = [lead for lead in store.leads(limit=50)
                     if lead.get("stage") in ("contacted", "replied", "objection")][:3]
            if not leads:
                return "Follow-up ke liye koi lead tayyar nahi (pehle outreach chalayein)."
            blocks = []
            for lead in leads:
                for touch in (1, 2, 3):
                    message = self.compose(lead, touch)
                    blocks.append("[%s | touch %d | %s]\n%s"
                                  % (lead.get("name"), touch, message["angle"],
                                     message["body"]))
            return "\n\n".join(blocks)

        if any(word in text for word in ("schedule", "plan", "queue")):
            count = 0
            for lead in store.leads(stage="contacted", limit=25):
                if self.kill_switch():
                    break
                count += len(self.schedule_sequence(int(lead["id"])))
            return "%d follow-ups schedule ho gaye (%d leads ke liye)." % (
                count, min(len(store.leads(stage="contacted", limit=25)), 25))

        results = self.send_due()
        if not results:
            return "Abhi koi follow-up due nahi hai."
        sent = sum(1 for r in results if r["sent"])
        return ("Follow-ups: %d due, %d sent.\n%s" % (
            len(results), sent,
            "\n".join("  - %s (touch %s, %s): %s" % (
                r["lead"], r["touch"], r["angle"], "sent" if r["sent"] else r["reason"][:40])
                for r in results[:10])))

    def run(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.send_due(limit=limit)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

#: Phrases that mean "this message has no new value" - the cardinal sin here.
NUDGE_PHRASES = ("just checking in", "just following up", "any update", "sirf yaad dilane",
                 "koi jawab", "reminder", "bas check kar raha", "kya socha",
                 "still interested", "please reply")


def _is_nudge(text: str) -> bool:
    """True when the message is a bare nudge with no value payload."""
    lowered = (text or "").lower()
    if len(lowered.split()) < 12:
        return True
    return any(phrase in lowered for phrase in NUDGE_PHRASES)


def _fmt(amount: Any) -> str:
    try:
        return "{:,}".format(int(amount))
    except Exception:
        return str(amount)
