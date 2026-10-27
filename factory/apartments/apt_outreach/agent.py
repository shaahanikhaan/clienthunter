"""apt_outreach - the trust opener.

The rules enforced here are not stylistic preferences, they are the whole point:

* NEVER open with "We build websites".  Open with a specific finding about
  THEIR site (the auditor supplies it).
* WhatsApp first, email only as backup.
* Max 20 sends/day - accounts get banned otherwise, and a banned number is
  worse than a slow week.
* Every message carries the business name and ends with one question.

TrustScriptTrainer (in :meth:`train`) learns the openers, objection replies and
closes from ``data/successful_conversations.json`` - the won ones become
templates, the lost ones become a "never do this" list.  A local n-gram model
trained on the same winning text lets the agent draft variations that still
sound like you.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ...store import default_store
from ..base_apartment import BaseApartment

PLACEHOLDERS = re.compile(r"\{(\w+)\}")


class OutreachAgent(BaseApartment):
    NAME = "outreach"
    DESCRIPTION = "Personalised first contact - always opens with a specific audit finding"
    PERMISSIONS = {"email": True, "whatsapp": True, "internet": True}

    TEMPLATES = "templates.json"
    GENERATOR = "generator.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        """TrustScriptTrainer: mine winning conversations for reusable scripts."""
        data = self.load_shared_data("successful_conversations.json", default={}) or {}
        conversations = data.get("conversations", [])
        if not conversations:
            raise ValueError("data/successful_conversations.json has no conversations")

        won = [c for c in conversations if c.get("outcome") == "won"]
        lost = [c for c in conversations if c.get("outcome") == "lost"]

        openers, closes, objection_replies, value_moves = [], [], [], []
        for conversation in won:
            messages = conversation.get("messages", [])
            lead = conversation.get("lead", {}) or {}
            for index, message in enumerate(messages):
                if message.get("from") != "agent":
                    continue
                text = (message.get("text") or "").strip()
                previous = messages[index - 1].get("text", "") if index else ""
                if index == 0:
                    openers.append({
                        "text": text,
                        "category": conversation.get("category", ""),
                        "channel": conversation.get("channel", "whatsapp"),
                        "value_pkr": conversation.get("value_pkr", 0),
                        "issue": lead.get("top_issue", ""),
                    })
                elif index == len(messages) - 1:
                    closes.append({"text": text, "value_pkr": conversation.get("value_pkr", 0)})
                elif previous and previous.strip().endswith(("?", "؟")) is False:
                    value_moves.append({"text": text})
            for move in conversation.get("winning_moves", []) or []:
                value_moves.append({"text": move, "kind": "note"})
            objection_replies.extend(self._pair_objections(messages))

        never = []
        for conversation in lost:
            never.extend(conversation.get("losing_moves", []) or [])
            messages = conversation.get("messages", [])
            if messages and messages[0].get("from") == "agent":
                never.append("OPENER: " + (messages[0].get("text") or "")[:140])

        model = {
            "openers": openers,
            "closes": closes,
            "value_moves": value_moves[:40],
            "objection_replies": objection_replies[:40],
            "never_do": never[:20],
            "stats": {"won": len(won), "lost": len(lost), "total": len(conversations),
                      "avg_deal_pkr": round(sum(c.get("value_pkr", 0) for c in won) /
                                            max(len(won), 1), 2)},
            "trained_at": time.time(),
        }
        self.save_model(self.TEMPLATES, model)

        # Local language model over the winning text: drafts keep your voice.
        corpus = [entry["text"] for entry in openers] + \
                 [entry["text"] for entry in closes] + \
                 [entry["text"] for entry in value_moves if entry.get("kind") != "note"]
        llm_stats = {}
        if corpus:
            llm = self.tool("llm_local")
            llm_stats = llm.finetune(corpus, n=3, min_count=1)

        self.remember("train_summary", {"openers": len(openers), "closes": len(closes),
                                        "never": len(never), **llm_stats})
        self.log("trained", openers=len(openers), closes=len(closes), corpus=len(corpus))
        return {"openers": len(openers), "closes": len(closes),
                "objection_replies": len(objection_replies),
                "never_do": len(never), "generator": llm_stats}

    @staticmethod
    def _pair_objections(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Lead said something negative, agent answered it -> a training pair."""
        pairs = []
        for index in range(1, len(messages)):
            previous, current = messages[index - 1], messages[index]
            if previous.get("from") == "lead" and current.get("from") == "agent":
                pairs.append({"objection": (previous.get("text") or "")[:200],
                              "reply": (current.get("text") or "")[:600]})
        return pairs

    # ---------------------------------------------------------------- compose
    def templates(self) -> Dict[str, Any]:
        return self.load_model(self.TEMPLATES, default={}) or {}

    def pick_opener(self, category: str = "", issue: str = "") -> str:
        """Choose the best-performing opener template for this situation."""
        model = self.templates()
        openers = model.get("openers") or []
        if not openers:
            return ("Salam {owner_name}, main ne {business_name} ki site dekhi. "
                    "{issue}. Is se {benefit}. {question}")
        scored = []
        for opener in openers:
            score = float(opener.get("value_pkr", 0)) / 1000.0
            if category and opener.get("category") == category:
                score += 8
            if issue and opener.get("issue") == issue:
                score += 5
            scored.append((score, opener["text"]))
        scored.sort(key=lambda kv: -kv[0])
        return scored[0][1]

    def compose(self, lead: Dict[str, Any], channel: str = "whatsapp") -> str:
        """Build one personalised first-contact message."""
        agency = (self.agency() or {}).get("agency", {}) or {}
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == lead.get("category")), {}) or {}

        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        if not issues:
            issues = [{"id": "unknown", "title": "site mobile pe theek nahi khulti",
                       "benefit": "customers 3 second mein chale jate hain",
                       "evidence": ""}]

        issue = issues[0]
        template = self.pick_opener(lead.get("category", ""), issue.get("id", ""))
        values = {
            "owner_name": lead.get("owner_name") or ", ".join(
                str(lead.get("name", "Sir")).split()[:2]),
            "business_name": lead.get("name", "aap ka business"),
            "city": lead.get("city", ""),
            "category": (lead.get("category", "") or "business").replace("_", " "),
            "issue": issue.get("title", ""),
            "benefit": issue.get("benefit", "customers quietly chale jate hain"),
            "evidence": issue.get("evidence", ""),
            "audit_score": lead.get("audit_score", 0),
            "issue_count": len(issues),
            "my_name": agency.get("owner_name", ""),
            "my_city": agency.get("city", ""),
            "years": agency.get("years_experience", 3),
            "question": "Kya main aap ko 2 minute mein explain karoon?",
            "proof_angle": category.get("proof_angle", ""),
            "seconds": self._seconds_from_issues(issues),
            "target": "1.2",
        }
        message = self._render(template, values)

        # Hard rule: it must open with the finding, never with our capability.
        if _opens_with_pitch(message):
            message = ("Salam %s, main ne %s ki site dekhi. %s Is se %s. %s"
                       % (values["owner_name"], values["business_name"],
                          values["issue"].capitalize(), values["benefit"],
                          values["question"]))
        message = self.enforce_limits(message, channel)
        return message

    @staticmethod
    def _seconds_from_issues(issues: List[Dict[str, Any]]) -> str:
        for issue in issues or []:
            evidence = str(issue.get("evidence", ""))
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:second|ms|s\b)", evidence)
            if match:
                return match.group(1)
        return "9"

    @staticmethod
    def _render(template: str, values: Dict[str, Any]) -> str:
        def replace(match):
            key = match.group(1)
            return str(values.get(key, match.group(0)))
        text = PLACEHOLDERS.sub(replace, template or "")
        return re.sub(r"\s+", " ", text).strip()

    def enforce_limits(self, message: str, channel: str = "whatsapp") -> str:
        rules = (self.agency() or {}).get("outreach_rules", {}) or {}
        if channel == "whatsapp":
            max_words = int(rules.get("whatsapp_max_words", 100))
        else:
            max_words = int(rules.get("email_max_words", 200))
        words = message.split()
        if len(words) > max_words:
            message = " ".join(words[:max_words]).rstrip(",;:") + "..."
        return message

    def validate(self, message: str, lead: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Pre-send guardrails.  Returns (ok, problems)."""
        problems = []
        lowered = (message or "").lower()
        for banned in ("we build websites", "hum website banate", "dear sir",
                       "i hope this email finds you well"):
            if banned in lowered:
                problems.append("generic pitch detected: %r" % banned)
        if lead.get("name") and str(lead["name"]).lower() not in lowered:
            problems.append("business name missing from message")
        if len(message.split()) > 120:
            problems.append("too long (%d words)" % len(message.split()))
        if not message.rstrip().endswith(("?", "؟")):
            problems.append("no question at the end")
        return (not problems), problems

    # ------------------------------------------------------------------ send
    def contact(self, lead: Dict[str, Any], channel: Optional[str] = None,
                dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Send the first contact to one lead.  Gate + quota enforced."""
        store = default_store()
        channel = channel or ("whatsapp" if lead.get("phone") else "email")
        message = self.compose(lead, channel)
        ok, problems = self.validate(message, lead)
        if not ok:
            self.log("message_rejected", lead=lead.get("name"), problems=problems)
            message = self._repair(message, lead, problems)

        result: Dict[str, Any] = {"lead": lead.get("name"), "channel": channel,
                                  "message": message, "ok": False}
        if channel == "whatsapp" and lead.get("phone"):
            result = self.tool("whatsapp").send_message(lead["phone"], message,
                                                        dry_run=dry_run)
            result.update({"lead": lead.get("name"), "channel": "whatsapp",
                           "message": message})
        elif lead.get("email"):
            subject = "%s: aap ki site ka ek masla" % lead.get("name", "Aap ki site")
            result = self.tool("email").send(lead["email"], subject, message,
                                             dry_run=dry_run)
            result.update({"lead": lead.get("name"), "channel": "email",
                           "message": message})
        else:
            result.update({"ok": False, "reason": "no phone and no email for this lead"})

        if result.get("ok"):
            if lead.get("id"):
                store.touch(int(lead["id"]))
                store.set_stage(int(lead["id"]), "contacted")
                store.log_message(int(lead["id"]), "out", result.get("channel", channel),
                                  message)
            self.log("contacted", lead=lead.get("name"), channel=result.get("channel"))
        else:
            self.log("contact_failed", lead=lead.get("name"), reason=result.get("reason"))
        return result

    def _repair(self, message: str, lead: Dict[str, Any], problems: List[str]) -> str:
        """Fix the most common violation: missing name or missing question."""
        if any("question" in p for p in problems):
            message = message.rstrip(". ") + ". Kya main 2 minute mein bataoon?"
        if any("name" in p for p in problems) and lead.get("name"):
            message = "Salam, %s ke bare mein: %s" % (lead["name"], message)
        return self.enforce_limits(message, "whatsapp")

    def run_batch(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Contact the best-scoring leads that have not been touched yet."""
        store = default_store()
        candidates = [lead for lead in store.leads(stage="new", limit=200)
                      if lead.get("phone") or lead.get("email")]
        candidates.sort(key=lambda lead: -int(lead.get("score") or 0))
        results = []
        for lead in candidates[:limit]:
            if self.kill_switch():
                break
            try:
                results.append(self.contact(lead))
            except Exception as exc:
                self.log("contact_error", lead=lead.get("name"), error=str(exc))
                results.append({"lead": lead.get("name"), "ok": False, "reason": str(exc)})
        sent = sum(1 for r in results if r.get("ok"))
        self.remember("last_batch", {"attempted": len(results), "sent": sent,
                                     "at": time.time()})
        return results

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").lower()
        store = default_store()
        limit = 20
        match = re.search(r"(\d{1,3})", text)
        if match:
            limit = max(1, min(int(match.group(1)), 20))

        if any(word in text for word in ("dikhao", "preview", "draft", "likho", "banao")):
            leads = [lead for lead in store.leads(limit=50)
                     if lead.get("phone") or lead.get("email")][:3]
            if not leads:
                return "Pehle hunter se leads laayein."
            blocks = []
            for lead in leads:
                if not lead.get("audit_issues"):
                    continue
                message = self.compose(lead, "whatsapp")
                ok, problems = self.validate(message, lead)
                blocks.append("[%s] %s\n%s\n(check: %s)" % (
                    lead.get("id"), lead.get("name"), message,
                    "theek hai" if ok else ", ".join(problems)))
            return "\n\n".join(blocks) or ("Audit pehle chalayein - outreach audit finding "
                                           "ke baghair nahi bhejta.")

        results = self.run_batch(limit=limit)
        sent = sum(1 for r in results if r.get("ok"))
        queued = sum(1 for r in results if r.get("queued"))
        lines = ["Outreach: %d attempts, %d sent, %d queued (offline/approval pending)."
                 % (len(results), sent, queued)]
        for result in results[:8]:
            status = "sent" if result.get("ok") else ("queued" if result.get("queued")
                                                      else "blocked")
            lines.append("  - %s [%s] %s" % (result.get("lead"), status,
                                             result.get("reason", "")[:60]))
        return "\n".join(lines)

    def run(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.run_batch(limit=limit)


def _opens_with_pitch(message: str) -> bool:
    head = (message or "")[:90].lower()
    return any(phrase in head for phrase in (
        "we build", "hum website", "hum banate", "i build websites",
        "website banate hain", "we create websites"))
