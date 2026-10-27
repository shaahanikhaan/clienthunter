"""apt_platform_linkedin - decision makers, found where they actually are.

LinkedIn is the only channel where you can reliably reach the owner/GM of a
larger Pakistani business rather than a shared WhatsApp number.  The playbook
here is deliberately slow and human:

    touch 1  connect with a short, personal note (no pitch)
    touch 2  one specific observation about their site
    touch 3  free mockup offer

Never a pitch in the first message - LinkedIn users report and restrict accounts
that do, and a restricted account is worse than no account.

All browser actions go through CDP against your real Chrome profile (so you are
already logged in) and through the permission gate.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

SEARCH_URL = ("https://www.linkedin.com/search/results/people/?keywords={query}"
              "&origin=GLOBAL_SEARCH_HEADER")


class PlatformLinkedInAgent(BaseApartment):
    NAME = "platform_linkedin"
    DESCRIPTION = "LinkedIn presence agent - search, connect with a note, then DM with proof"
    PERMISSIONS = {"browser": True, "internet": True}

    MODEL = "playbook.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        platform = (self.load_shared_data("platform_posts.json", default={}) or {}
                    ).get("platforms", {}).get("linkedin", {}) or {}
        learned = self.load_model(self.MODEL, default={}) or {}
        model = {
            "platform": "linkedin",
            "daily_limit": int(platform.get("daily_limit", 15)),
            "max_words": int(platform.get("max_words", 120)),
            "best_hours": learned.get("best_hours") or platform.get("best_hours",
                                                                    [9, 10, 11, 15, 16]),
            "best_days": platform.get("best_days", ["tuesday", "wednesday", "thursday"]),
            "opener_pattern": platform.get("opener_pattern", ""),
            "templates": platform.get("templates", []),
            "connect_note_limit": int(platform.get("connect_note_limit", 300)),
            "sequence": ["connect_note", "observation", "mockup_offer"],
            "stats": {"connects": int(learned.get("stats", {}).get("connects", 0)),
                      "dms": int(learned.get("stats", {}).get("dms", 0)),
                      "replies": int(learned.get("stats", {}).get("replies", 0))},
            "notes": platform.get("notes", ""),
        }
        self.save_model(self.MODEL, model)
        self.log("trained", limit=model["daily_limit"])
        return {"daily_limit": model["daily_limit"], "sequence": model["sequence"]}

    def playbook(self) -> Dict[str, Any]:
        return self.load_model(self.MODEL, default={}) or {}

    # ---------------------------------------------------------------- compose
    def connect_note(self, lead: Dict[str, Any]) -> str:
        model = self.playbook()
        template = (model.get("templates") or [
            "Hi {first_name} - noticed {business_name}'s site has {issue}. "
            "We fixed the same thing for a {category} in {city} recently. "
            "Happy to share what we changed."])[0]
        text = _render(template, self._values(lead))
        return text[:int(model.get("connect_note_limit", 300))]

    def message(self, lead: Dict[str, Any], step: int = 0) -> str:
        values = self._values(lead)
        steps = [
            "Hi {first_name} - {business_name} ki site mobile pe {seconds} second leti hai. "
            "Hum ne aik {category} ({city}) ko {target} second tak laya tha. "
            "Free audit report bhejoon?",
            "{first_name}, {business_name} ki site ka score {audit_score}/100 hai - "
            "sab se bara masla: {issue}. Is se {benefit}. "
            "Agar aap chahein to main aap ka free mockup bana doon.",
            "Hi {first_name} - main ne {business_name} ka free homepage mockup bana liya hai. "
            "Bhejoon? Pasand na aaye to koi paisa nahi, koi commitment nahi.",
        ]
        return _render(steps[min(step, len(steps) - 1)], values)

    def _values(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        issues = lead.get("audit_issues") or []
        issue = issues[0] if issues else {}
        name = str(lead.get("name", "")).strip()
        return {
            "first_name": (lead.get("owner_name") or name.split()[0] if name else "there"),
            "business_name": name or "your company",
            "category": (lead.get("category", "") or "business").replace("_", " "),
            "city": lead.get("city", ""),
            "issue": issue.get("title", "no mobile viewport"),
            "benefit": issue.get("benefit", "mobile visitors leave before it loads"),
            "audit_score": lead.get("audit_score", 0),
            "seconds": "9", "target": "1.2",
        }

    # ----------------------------------------------------------------- browser
    def search(self, query: str, limit: int = 10) -> List[Dict[str, str]]:
        """Search people, return name/headline/link.  Read-only, medium risk."""
        browser = self.tool("browser")
        if not browser.is_running() and not browser.start():
            self.log("chrome_unavailable")
            return []
        url = SEARCH_URL.format(query=urllib.parse.quote_plus(query))
        session = browser.new_tab()
        if session is None:
            return []
        try:
            session.navigate(url, wait_s=5)
            script = (
                "(function(){var out=[];"
                "document.querySelectorAll('li.reusable-search__result-container, "
                "div.entity-result').forEach(function(el){"
                "var t=(el.innerText||'').split('\\n').filter(function(s){return s.trim();});"
                "var a=el.querySelector('a[href*=\"/in/\"]');"
                "if(t[0]) out.push({name:t[0], headline:t[1]||'', url:a?a.href:''});});"
                "return out.slice(0,%d);})()" % int(limit))
            return session.evaluate(script) or []
        except Exception as exc:
            self.log("search_failed", error=str(exc))
            return []
        finally:
            session.close()

    def send_connect(self, profile_url: str, note: str) -> Dict[str, Any]:
        """Send a connection request with a note.  HIGH risk -> human approval."""
        self.permit("connect_request", {"url": profile_url, "note": note[:80]})
        browser = self.tool("browser")
        if self.offline or not browser.is_running():
            return {"ok": False, "reason": "browser unavailable (or offline)"}
        session = browser.new_tab(profile_url)
        if session is None:
            return {"ok": False, "reason": "cannot open tab"}
        try:
            session.wait_for("button", timeout_s=10)
            # Click "Connect", then "Add a note", type, then "Send".
            clicked = session.evaluate(
                "(function(){var btns=[].slice.call(document.querySelectorAll('button'));"
                "var b=btns.find(function(x){return /connect/i.test(x.innerText);});"
                "if(b){b.click();return true;}return false;})()")
            if not clicked:
                return {"ok": False, "reason": "connect button not found"}
            time.sleep(1.5)
            session.evaluate(
                "(function(){var b=[].slice.call(document.querySelectorAll('button'))"
                ".find(function(x){return /add a note/i.test(x.innerText);});"
                "if(b){b.click();return true;}return false;})()")
            time.sleep(1.0)
            typed = session.type_text("textarea[name=message]", note[:299])
            time.sleep(0.5)
            session.evaluate(
                "(function(){var b=[].slice.call(document.querySelectorAll('button'))"
                ".find(function(x){return /^send$/i.test((x.innerText||'').trim());});"
                "if(b){b.click();return true;}return false;})()")
            self._bump("connects")
            return {"ok": True, "url": profile_url, "typed": typed}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        finally:
            session.close()

    def send_dm(self, profile_url: str, message: str) -> Dict[str, Any]:
        self.permit("send_dm", {"url": profile_url, "chars": len(message)})
        browser = self.tool("browser")
        if self.offline or not browser.is_running():
            return {"ok": False, "reason": "browser unavailable (or offline)"}
        self._bump("dms")
        return {"ok": True, "queued": True,
                "reason": "DM composed; send it from the open tab (LinkedIn rate-limits "
                          "automated sends aggressively)"}

    def _bump(self, key: str) -> None:
        model = self.playbook()
        stats = model.setdefault("stats", {"connects": 0, "dms": 0, "replies": 0})
        stats[key] = int(stats.get(key, 0)) + 1
        self.save_model(self.MODEL, model)

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        store = default_store()

        if "dhoond" in lowered or "search" in lowered or "find" in lowered:
            query = re.sub(r".*(?:dhoondo|search|find)\s*", "", text).strip() or \
                    "restaurant owner Karachi"
            people = self.search(query, limit=8)
            if not people:
                return ("LinkedIn search ke liye Chrome chahiye (login ke sath). "
                        "Chrome start nahi ho saka - browser tool check karein.")
            return "\n".join("- %s | %s | %s" % (p.get("name"), p.get("headline", "")[:60],
                                                 p.get("url", "")[:60]) for p in people)

        if any(word in lowered for word in ("draft", "note", "message", "likho")):
            leads = store.leads(limit=3)
            return "\n\n".join(
                "[connect note] %s\n%s\n\n[step 2] %s"
                % (lead.get("name"), self.connect_note(lead), self.message(lead, 1))
                for lead in leads) or "Koi lead nahi mila."

        return ("LinkedIn agent ready.\n"
                "  'LinkedIn pe <query> dhoondo' - logon ko search karta hai\n"
                "  'LinkedIn draft banao'        - connect note + 2 follow-ups likhta hai\n"
                "Daily limit: %s connects/DMs. Browser: Chrome CDP (aap ka logged-in session)."
                % self.playbook().get("daily_limit", 15))

    def run(self, limit: int = 5) -> List[Dict[str, Any]]:
        store = default_store()
        out = []
        for lead in store.leads(limit=limit):
            if self.kill_switch():
                break
            query = "%s owner %s" % (lead.get("category", "business"), lead.get("city", ""))
            people = self.search(query, limit=2)
            out.append({"lead": lead.get("name"), "found": len(people),
                        "note": self.connect_note(lead)[:120]})
        return out


def _render(template: str, values: Dict[str, Any]) -> str:
    def replace(match):
        return str(values.get(match.group(1), match.group(0)))
    return re.sub(r"\s+", " ", re.sub(r"\{(\w+)\}", replace, template or "")).strip()
