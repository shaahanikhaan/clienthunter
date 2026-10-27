"""apt_inbox - reads replies and decides what happens next.

Pulls from Gmail over IMAP (App Password, no OAuth project) and from WhatsApp
Web through the CDP session, then classifies each reply with the Urdu NLP brain:

    interest           -> apt_requirements (build the brief) / apt_closer
    objection          -> apt_convincer
    price_query        -> apt_closer (quote)
    requirement_query  -> apt_requirements
    callback_request   -> apt_closer (schedule the call)
    human_needed       -> flagged for you, never auto-answered
    not_interested     -> marked lost, follow-ups stopped

Nothing is auto-sent from here.  The inbox routes; the closer/convincer/followup
apartments do the talking, and they still go through the permission gate.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from ...store import default_store
from ..base_apartment import BaseApartment

#: intent -> where the reply is routed
ROUTING = {
    "interest": "requirements",
    "requirement_query": "requirements",
    "objection": "convincer",
    "price_query": "closer",
    "callback_request": "closer",
    "human_needed": "human",
    "not_interested": "closed",
}


class InboxAgent(BaseApartment):
    NAME = "inbox"
    DESCRIPTION = "Reads email + WhatsApp replies, classifies them, and routes each one"
    PERMISSIONS = {"email": True, "internet": True, "whatsapp": True}

    MODEL = "urdu_nlp.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        """Its own private copy of the Urdu model (apartments share no files)."""
        data = self.load_shared_data("urdu_training.json", default={}) or {}
        examples = data.get("examples", [])
        if not examples:
            raise ValueError("data/urdu_training.json missing")
        from ..tools.nlp_urdu import UrduNlpModel
        model = UrduNlpModel.train(examples)
        self.save_model(self.MODEL, model.to_dict())
        self.save_model("routing.json",
                        {"routing": ROUTING, "intents": sorted(ROUTING)})
        self.log("trained", examples=len(examples))
        return {"examples": len(examples), "routing": len(ROUTING)}

    def nlp(self):
        return self.tool("nlp_urdu")

    # ------------------------------------------------------------------ fetch
    def fetch_emails(self, limit: int = 30) -> List[Dict[str, Any]]:
        if self.offline or not self.can("email"):
            return []
        return self.tool("email").read(limit=limit, unseen_only=True)

    def fetch_whatsapp(self, limit: int = 20) -> List[Dict[str, Any]]:
        if self.offline or not self.can("whatsapp"):
            return []
        return self.tool("whatsapp").read_messages(limit=limit)

    # -------------------------------------------------------------- classify
    def classify(self, text: str) -> Dict[str, Any]:
        analysis = self.nlp().analyse(text)
        analysis["route"] = ROUTING.get(analysis.get("intent", ""), "requirements")
        return analysis

    def match_lead(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Attach a reply to a lead by email, phone number, or business name."""
        store = default_store()
        address = (message.get("from") or "").lower()
        email_match = None
        import re
        match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", address)
        if match:
            email_match = match.group(0)
        if email_match:
            for lead in store.leads(limit=500):
                if (lead.get("email") or "").lower() == email_match:
                    return lead
        body = (message.get("text") or message.get("body") or "") + " " + address
        for lead in store.leads(limit=500):
            name = str(lead.get("name", "")).lower()
            if len(name) > 4 and name in body.lower():
                return lead
        return None

    # ---------------------------------------------------------------- process
    def process_replies(self, limit: int = 30) -> List[Dict[str, Any]]:
        """The daily-cycle entry point: read, classify, store, route."""
        store = default_store()
        processed: List[Dict[str, Any]] = []
        if self.kill_switch():
            return processed

        items: List[Dict[str, Any]] = []
        for message in self.fetch_emails(limit):
            items.append({"channel": "email", "from": message.get("from", ""),
                          "text": message.get("body", ""),
                          "subject": message.get("subject", ""),
                          "message_id": message.get("message_id", "")})
        for message in self.fetch_whatsapp(limit):
            if message.get("direction") == "out":
                continue
            items.append({"channel": "whatsapp", "from": message.get("from", ""),
                          "text": message.get("text", ""), "subject": "",
                          "message_id": ""})

        for item in items:
            if self.kill_switch():
                break
            text = (item.get("text") or "").strip()
            if not text:
                continue
            analysis = self.classify(text)
            lead = self.match_lead(item)
            record = {
                "channel": item["channel"],
                "from": item["from"],
                "text": text[:600],
                "intent": analysis.get("intent"),
                "sentiment": analysis.get("sentiment"),
                "priority": analysis.get("priority"),
                "route": analysis.get("route"),
                "entities": analysis.get("entities", {}),
                "lead_id": lead.get("id") if lead else None,
                "lead_name": lead.get("name") if lead else None,
                "received_at": time.time(),
            }
            if lead:
                store.log_message(int(lead["id"]), "in", item["channel"], text[:600],
                                  intent=record["intent"], sentiment=record["sentiment"],
                                  priority=record["priority"])
                self._apply_stage(store, lead, record)
            processed.append(record)
            self.log("reply", **{k: record[k] for k in
                                 ("intent", "sentiment", "priority", "route", "lead_id")})

        if processed:
            self.remember("last_poll", {"at": time.time(), "count": len(processed)})
            store.log_event("inbox", "replies_processed", {"count": len(processed)})
        return processed

    def _apply_stage(self, store, lead: Dict[str, Any], record: Dict[str, Any]) -> None:
        intent = record.get("intent")
        lead_id = int(lead["id"])
        if intent == "not_interested":
            store.set_stage(lead_id, "lost")
        elif intent in ("interest", "callback_request", "price_query"):
            store.set_stage(lead_id, "replied",
                            score=min(100, int(lead.get("score", 0)) + 15))
        elif intent == "objection":
            store.set_stage(lead_id, "objection",
                            score=min(100, int(lead.get("score", 0)) + 8))
        elif intent == "human_needed":
            store.set_stage(lead_id, "human_needed",
                            score=min(100, int(lead.get("score", 0)) + 10))
        else:
            store.set_stage(lead_id, "engaged",
                            score=min(100, int(lead.get("score", 0)) + 5))

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()

        # "is message ko samjho / classify karo"
        if any(word in lowered for word in ("samjho", "classify", "matlab", "intent")):
            target = text
            for marker in ("samjho", "classify", "matlab", "intent"):
                target = target.replace(marker, "")
            target = target.strip(" :,-")
            if not target:
                return "Konsa message samjhoon? Text do."
            analysis = self.classify(target)
            return ("Intent: %s (%.0f%%)\nSentiment: %s\nPriority: %s\nRoute: apt_%s\n%s"
                    % (analysis["intent"], analysis["intent_confidence"] * 100,
                       analysis["sentiment"], analysis["priority"], analysis["route"],
                       self._entities_line(analysis.get("entities", {}))))

        replies = self.process_replies()
        if not replies:
            if self.offline:
                return ("Offline mode: email/WhatsApp read nahi ho sakta. "
                        "config/email.json bhar kar aur internet on kar ke dobara chalaayein.")
            return "Koi naya reply nahi hai."
        return self.format_replies(replies)

    @staticmethod
    def _entities_line(entities: Dict[str, Any]) -> str:
        bits = []
        if entities.get("prices"):
            bits.append("price: " + ", ".join("%s %s" % (p["value"], p["currency"])
                                              for p in entities["prices"][:2]))
        if entities.get("phones"):
            bits.append("phone: " + ", ".join(entities["phones"][:2]))
        if entities.get("dates"):
            bits.append("date: " + ", ".join(str(d.get("raw")) for d in entities["dates"][:2]))
        if entities.get("names"):
            bits.append("name: " + ", ".join(entities["names"][:2]))
        return ("Entities -> " + "; ".join(bits)) if bits else ""

    def format_replies(self, replies: List[Dict[str, Any]]) -> str:
        order = {"high": 0, "medium": 1, "low": 2}
        replies = sorted(replies, key=lambda r: order.get(r.get("priority"), 1))
        lines = ["%d replies:" % len(replies)]
        for reply in replies[:20]:
            lead = reply.get("lead_name") or "unknown lead"
            lines.append("[%s] %s (%s) -> %s | sentiment %s | priority %s"
                         % (reply["channel"], lead, reply["intent"], reply["route"],
                            reply["sentiment"], reply["priority"]))
            lines.append("     \"%s\"" % (reply["text"][:160].replace("\n", " ")))
        human = [r for r in replies if r["intent"] == "human_needed"]
        if human:
            lines.append("")
            lines.append("** %d reply hain jahan insaan chahiye - aap khud jawab dein. **" % len(human))
        return "\n".join(lines)

    def run(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self.process_replies(limit=limit)
