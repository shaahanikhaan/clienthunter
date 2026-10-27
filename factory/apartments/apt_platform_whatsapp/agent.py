"""apt_platform_whatsapp - the primary channel in Pakistan.

WhatsApp converts better than email here by a wide margin, which is exactly why
it needs the strictest guardrails:

*   20 messages/day hard cap (ban protection - a lost number is unrecoverable),
*   recipient allowlist,
*   ``send_whatsapp`` is HIGH risk, so a human approves it (or it is denied when
    the factory runs headless),
*   every message <= 100 words and opens with a specific finding.

Engine selection is automatic: ``waeys`` (pure-Python WhatsApp Web protocol
client, no Node.js) when installed, otherwise WhatsApp Web driven through your
real Chrome over CDP, otherwise the offline outbox.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment


class PlatformWhatsAppAgent(BaseApartment):
    NAME = "platform_whatsapp"
    DESCRIPTION = "WhatsApp outreach - 20/day cap, allowlisted recipients, proof-first messages"
    PERMISSIONS = {"whatsapp": True, "browser": True, "internet": True}

    MODEL = "playbook.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        platform = (self.load_shared_data("platform_posts.json", default={}) or {}
                    ).get("platforms", {}).get("whatsapp", {}) or {}
        conversations = (self.load_shared_data("successful_conversations.json",
                                               default={}) or {}).get("conversations", [])
        wa_wins = [c for c in conversations
                   if c.get("channel") == "whatsapp" and c.get("outcome") == "won"]

        learned = self.load_model(self.MODEL, default={}) or {}
        model = {
            "platform": "whatsapp",
            "daily_limit": int(platform.get("daily_limit", 20)),
            "max_words": int(platform.get("max_words", 100)),
            "best_hours": learned.get("best_hours") or platform.get("best_hours", [10, 11, 17, 18]),
            "best_days": platform.get("best_days", ["monday", "tuesday", "wednesday", "thursday"]),
            "avoid_hours": platform.get("avoid_hours", [0, 1, 2, 3, 4, 5, 6]),
            "templates": platform.get("templates", []),
            "opener_pattern": platform.get("opener_pattern", ""),
            "followup_gap_days": platform.get("followup_gap_days", [2, 4, 7, 11, 16, 23]),
            "stats": {"sent": int(learned.get("stats", {}).get("sent", 0)),
                      "replied": int(learned.get("stats", {}).get("replied", 0)),
                      "won": int(learned.get("stats", {}).get("won", 0)),
                      "winning_examples": len(wa_wins)},
            "notes": platform.get("notes", ""),
        }
        self.save_model(self.MODEL, model)
        self.log("trained", limit=model["daily_limit"], wins=len(wa_wins))
        return {"daily_limit": model["daily_limit"], "winning_examples": len(wa_wins)}

    def playbook(self) -> Dict[str, Any]:
        return self.load_model(self.MODEL, default={}) or {}

    def in_window(self) -> bool:
        """Respect Pakistani working hours - 3 AM messages destroy trust."""
        model = self.playbook()
        hour = int(time.strftime("%H"))
        return hour not in (model.get("avoid_hours") or [0, 1, 2, 3, 4, 5, 6])

    # ---------------------------------------------------------------- compose
    def compose(self, lead: Dict[str, Any]) -> str:
        model = self.playbook()
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        issue = issues[0] if issues else {}
        templates = model.get("templates") or []
        template = templates[0] if templates else (
            "Salam {owner_name}, main ne {business_name} ki site dekhi - {issue}. "
            "Is se {benefit}. {question}")
        values = {
            "owner_name": lead.get("owner_name") or "bhai",
            "business_name": lead.get("name", "aap ka business"),
            "issue": issue.get("title", "site mobile pe theek nahi khulti"),
            "benefit": issue.get("benefit", "customers quietly chale jate hain"),
            "question": "Kya main 2 minute mein bataoon?",
            "category": (lead.get("category", "") or "business").replace("_", " "),
            "seconds": "9",
        }
        text = _render(template, values)
        words = text.split()
        if len(words) > int(model.get("max_words", 100)):
            text = " ".join(words[:int(model.get("max_words", 100))]) + "..."
        return text

    # ------------------------------------------------------------------- send
    def send(self, lead: Dict[str, Any], message: Optional[str] = None,
             dry_run: Optional[bool] = None) -> Dict[str, Any]:
        message = message or self.compose(lead)
        if not lead.get("phone"):
            return {"ok": False, "reason": "lead has no phone number"}
        result = self.tool("whatsapp").send_message(lead["phone"], message, dry_run=dry_run)
        if result.get("ok") and lead.get("id"):
            store = default_store()
            store.log_message(int(lead["id"]), "out", "whatsapp", message)
            store.touch(int(lead["id"]))
            self._bump("sent")
        return {"lead": lead.get("name"), "message": message, **result}

    def broadcast(self, leads: List[Dict[str, Any]], limit: int = 20
                  ) -> List[Dict[str, Any]]:
        results = []
        for lead in leads[:limit]:
            if self.kill_switch():
                break
            if not self.in_window():
                results.append({"lead": lead.get("name"), "ok": False,
                                "reason": "outside working hours"})
                continue
            try:
                results.append(self.send(lead))
            except Exception as exc:
                results.append({"lead": lead.get("name"), "ok": False, "reason": str(exc)})
        return results

    def _bump(self, key: str) -> None:
        model = self.playbook()
        stats = model.setdefault("stats", {"sent": 0, "replied": 0, "won": 0})
        stats[key] = int(stats.get(key, 0)) + 1
        self.save_model(self.MODEL, model)

    def record_reply(self) -> None:
        self._bump("replied")

    def record_win(self) -> None:
        self._bump("won")

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").lower()
        store = default_store()
        if any(word in text for word in ("dikhao", "preview", "draft", "compose")):
            leads = [lead for lead in store.leads(limit=50) if lead.get("phone")][:3]
            return "\n\n".join("[%s]\n%s" % (lead.get("name"), self.compose(lead))
                               for lead in leads) or "Koi lead with phone nahi mila."

        limit = 20
        import re
        match = re.search(r"(\d{1,2})", text)
        if match:
            limit = max(1, min(int(match.group(1)), 20))
        leads = [lead for lead in store.leads(stage="new", limit=100) if lead.get("phone")]
        leads.sort(key=lambda lead: -int(lead.get("score") or 0))
        results = self.broadcast(leads, limit=limit)
        sent = sum(1 for r in results if r.get("ok"))
        queued = sum(1 for r in results if r.get("queued"))
        tool = self.tool("whatsapp")
        return ("WhatsApp: %d/%d sent, %d queued. Backend: %s | window ok: %s\n%s"
                % (sent, len(results), queued, tool.backend, self.in_window(),
                   "\n".join("  - %s: %s" % (r.get("lead"),
                                             "sent" if r.get("ok") else
                                             ("queued" if r.get("queued") else
                                              str(r.get("reason"))[:50]))
                             for r in results[:10])))

    def run(self, limit: int = 20) -> List[Dict[str, Any]]:
        store = default_store()
        leads = [lead for lead in store.leads(stage="new", limit=100) if lead.get("phone")]
        leads.sort(key=lambda lead: -int(lead.get("score") or 0))
        return self.broadcast(leads, limit=limit)


def _render(template: str, values: Dict[str, Any]) -> str:
    import re
    def replace(match):
        return str(values.get(match.group(1), match.group(0)))
    return re.sub(r"\s+", " ", re.sub(r"\{(\w+)\}", replace, template or "")).strip()
