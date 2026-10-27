"""apt_enricher - turns a bare business name into a contactable lead.

Six extraction layers run cheapest-first (see tools/enrich.py):
mailto -> regex -> Cloudflare decode -> JSON-LD -> obfuscated text -> contact page.

It also learns which email patterns actually work in Pakistan: ``info@`` is not
always the winner, and the prior table in ``models/email_patterns.json`` updates
each time an extracted address turns out to be real (someone replies).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment


class EnricherAgent(BaseApartment):
    NAME = "enricher"
    DESCRIPTION = "Extracts emails, phones and socials from lead websites (6-layer extraction)"
    PERMISSIONS = {"internet": True, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "email_patterns.json"

    #: Priors before any learning - Pakistani SMEs really do favour info@/contact@.
    DEFAULT_PATTERNS = {
        "info": 0.30, "contact": 0.24, "sales": 0.16, "admin": 0.10,
        "hello": 0.08, "support": 0.06, "office": 0.06,
    }

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        learned = self.load_model(self.MODEL, default={}) or {}
        patterns = learned.get("patterns") or dict(self.DEFAULT_PATTERNS)
        # keep any patterns the trainer discovered, seed the rest
        for key, value in self.DEFAULT_PATTERNS.items():
            patterns.setdefault(key, value)
        total = sum(patterns.values()) or 1.0
        normalised = {k: round(v / total, 4) for k, v in patterns.items()}
        model = {
            "patterns": normalised,
            "observations": int(learned.get("observations", 0)),
            "found": int(learned.get("found", 0)),
            "notes": "Prior probability that a Pakistani SME uses a given email prefix. "
                     "Updated by record_outcome() when a sent email gets a reply.",
        }
        self.save_model(self.MODEL, model)
        self.log("trained", patterns=len(normalised))
        return model

    def patterns(self) -> Dict[str, float]:
        model = self.load_model(self.MODEL, default={}) or {}
        return model.get("patterns") or dict(self.DEFAULT_PATTERNS)

    def best_pattern(self) -> str:
        patterns = self.patterns()
        return max(patterns, key=patterns.get) if patterns else "info"

    # ----------------------------------------------------------------- enrich
    def enrich(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch the lead's site and fill in contact data.  Updates the DB."""
        tool = self.tool("enrich")
        result = tool.enrich(lead)
        update: Dict[str, Any] = {"site_status": result.get("site_status", "unknown")}
        if result.get("email"):
            update["email"] = result["email"]
        phones = [p for p in (result.get("phones") or []) if p]
        if phones:
            update["phone"] = phones[0] or lead.get("phone", "")
        if result.get("socials"):
            update["socials"] = result["socials"]
        store = default_store()
        if lead.get("id"):
            store.update_fields(int(lead["id"]), **update)
            # A reachable phone/email is worth score: it is contactable.
            bonus = 0
            if update.get("email"):
                bonus += 8
            if update.get("phone"):
                bonus += 5
            if update.get("site_status") == "live":
                bonus += 3
            if bonus:
                store.update_fields(int(lead["id"]),
                                    score=min(100, int(lead.get("score", 0)) + bonus))
            store.log_event("enricher", "lead_enriched",
                            {"lead_id": lead["id"], "email": bool(update.get("email")),
                             "status": update.get("site_status")})
        self.remember("last_enriched", {"lead": lead.get("name", ""),
                                        "status": update.get("site_status")})
        return result

    def enrich_batch(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Enrich every lead that has a website but no email yet."""
        store = default_store()
        leads = store.leads(limit=500)
        todo = [lead for lead in leads
                if lead.get("website") and not lead.get("email")][:limit]
        results = []
        for lead in todo:
            if self.kill_switch():
                break
            if self.offline:
                break
            try:
                results.append({"lead": lead.get("name"), **self.enrich(lead)})
            except Exception as exc:
                self.log("enrich_failed", lead=lead.get("name"), error=str(exc))
        return results

    def record_outcome(self, email: str, replied: bool) -> Dict[str, float]:
        """Learn from reality: did this prefix actually reach a human?"""
        prefix = (email or "").split("@", 1)[0].lower()
        prefix = "".join(ch for ch in prefix if ch.isalpha())[:12]
        if not prefix:
            return self.patterns()
        model = self.load_model(self.MODEL, default={}) or {}
        patterns = dict(model.get("patterns") or self.DEFAULT_PATTERNS)
        current = patterns.get(prefix, 0.05)
        patterns[prefix] = round(max(0.01, current + (0.04 if replied else -0.02)), 4)
        total = sum(patterns.values()) or 1.0
        model["patterns"] = {k: round(v / total, 4) for k, v in patterns.items()}
        model["observations"] = int(model.get("observations", 0)) + 1
        model["found"] = int(model.get("found", 0)) + (1 if replied else 0)
        self.save_model(self.MODEL, model)
        return model["patterns"]

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").lower()
        store = default_store()
        if "search" in text or "dhoond" in text:
            needle = ""
            match = None
            for word in text.split():
                if len(word) > 3 and word not in ("dhoondo", "dhondo", "search", "find",
                                                  "the", "karo", "karein"):
                    match = word
                    break
            needle = match or ""
            leads = store.search_leads(needle, limit=10) if needle else store.leads(limit=10)
            return "\n".join("#%s %s (%s, %s) %s" % (
                lead.get("id"), lead.get("name"), lead.get("city", ""),
                lead.get("category", ""), lead.get("email") or "no-email")
                for lead in leads) or "Koi lead nahi mila."

        results = self.enrich_batch(limit=int(self.config.get("limits", {}).get("max_pages_per_lead", 3)) * 7)
        if not results:
            if self.offline:
                return ("Offline mode: enrichment ke liye internet chahiye. "
                        "Internet on kar ke dobara chalaayein.")
            return "Sab leads enriched ho chuke hain, ya jin ke paas website hai un ka email already hai."
        lines = ["%d leads enrich kiye gaye:" % len(results)]
        for result in results[:15]:
            lines.append("  - %s | email: %s | phones: %s | status: %s" % (
                result.get("lead", "?"), result.get("email") or "-",
                ", ".join(result.get("phones") or []) or "-", result.get("site_status")))
        return "\n".join(lines)

    def run(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.enrich_batch(limit=limit)
