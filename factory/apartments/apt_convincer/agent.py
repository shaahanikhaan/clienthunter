"""apt_convincer - the trust builder.  The most important apartment here.

Pakistani SME owners do not say "no" because of price.  They say no because they
have been burned before, or because you gave them nothing concrete to believe.
So every single reply from this apartment contains a **specific proof point** -
a number from their own site, a measurable result from a named client, or a
concrete de-risking offer.  Generic reassurance is treated as a bug.

Objection handling is learned: each response template carries a success rate
that apt_self_trainer updates from your won/lost outcomes, and the highest-rated
response for the detected objection is the one that gets used.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ...store import default_store
from ..base_apartment import BaseApartment


class ConvincerAgent(BaseApartment):
    NAME = "convincer"
    DESCRIPTION = "Trust building and objection handling - every reply carries specific proof"
    PERMISSIONS = {"internet": False, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "objections.json"
    SCRIPTS = "templates.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        data = self.load_shared_data("objections.json", default={}) or {}
        objections = data.get("objections", [])
        if not objections:
            raise ValueError("data/objections.json has no objections")

        # Fold in what actually worked in real conversations.
        conversations = (self.load_shared_data("successful_conversations.json",
                                               default={}) or {}).get("conversations", [])
        learned = self.load_model(self.MODEL, default={}) or {}
        rates = dict(learned.get("success_rates") or {})

        for conversation in conversations:
            won = conversation.get("outcome") == "won"
            for index in range(1, len(conversation.get("messages", []))):
                pair = conversation["messages"][index - 1:index + 1]
                if len(pair) < 2 or pair[0].get("from") != "lead":
                    continue
                objection_id = self.match_objection(pair[0].get("text", ""))[0]
                if not objection_id:
                    continue
                key = "%s::%s" % (objection_id, self._style_of(pair[1].get("text", "")))
                current = rates.get(key, 0.5)
                rates[key] = round(current + (0.25 if won else -0.2) *
                                   (1 - abs(current - (1.0 if won else 0.0))) * 0.6, 4)

        payload = {
            "objections": objections,
            "success_rates": rates,
            "signal_index": self._build_signal_index(objections),
            "stats": {"objection_types": len(objections),
                      "learned_pairs": len(rates),
                      "conversations": len(conversations)},
            "trained_at": time.time(),
        }
        self.save_model(self.MODEL, payload)

        # Trust scripts: winning moves + proof lines, for reuse and for the
        # local generator so drafts keep your phrasing.
        scripts = {"proof_lines": [], "trust_lines": [], "closes": []}
        for conversation in conversations:
            if conversation.get("outcome") != "won":
                continue
            scripts["proof_lines"].extend(conversation.get("winning_moves", []) or [])
            for message in conversation.get("messages", []):
                if message.get("from") == "agent":
                    scripts["closes"].append((message.get("text") or "")[:400])
        self.save_model(self.SCRIPTS, scripts)

        corpus = [line for line in scripts["closes"] if len(line.split()) > 8]
        generator_stats = {}
        if corpus:
            generator_stats = self.tool("llm_local").finetune(corpus, n=3)

        self.log("trained", objections=len(objections), pairs=len(rates))
        return {"objection_types": len(objections), "learned_pairs": len(rates),
                "generator": generator_stats}

    @staticmethod
    def _build_signal_index(objections: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        return {obj["id"]: [str(s).lower() for s in obj.get("signals", [])]
                for obj in objections}

    @staticmethod
    def _style_of(text: str) -> str:
        lowered = (text or "").lower()
        if any(word in lowered for word in ("monthly", "mahine")):
            return "monthly"
        if any(word in lowered for word in ("mockup", "free", "trial")):
            return "trial"
        if any(word in lowered for word in ("basic", "recommended", "premium")):
            return "tiered"
        if any(word in lowered for word in ("portfolio", "reference", "result")):
            return "proof_first"
        return "general"

    # --------------------------------------------------------------- matching
    def model(self) -> Dict[str, Any]:
        return self.load_model(self.MODEL, default={}) or {}

    def match_objection(self, text: str) -> Tuple[Optional[str], float]:
        """Which objection is this?  Returns (id, confidence)."""
        payload = self.model()
        index = payload.get("signal_index") or {}
        lowered = (text or "").lower()
        best, score = None, 0.0
        for objection_id, signals in index.items():
            hits = sum(1 for signal in signals if signal in lowered)
            if hits:
                weighted = hits / max(len(signals) ** 0.5, 1.0)
                if weighted > score:
                    best, score = objection_id, weighted
        if best is None:
            return None, 0.0
        return best, round(min(1.0, score / 2.0), 3)

    def best_response(self, objection_id: str) -> Dict[str, Any]:
        """Highest success-rate template for this objection, style included."""
        payload = self.model()
        rates = payload.get("success_rates") or {}
        objection = next((o for o in payload.get("objections", [])
                          if o.get("id") == objection_id), None)
        if objection is None:
            return {"text": "Samajh gaya hoon. Ek cheez clear kar lein: main pehle aap ka "
                            "free mockup bana kar bhejta hoon - pasand aaye tab aage baat.",
                    "style": "trial", "success_rate": 0.3}
        responses = list(objection.get("responses", []))
        for response in responses:
            key = "%s::%s" % (objection_id, response.get("style", "general"))
            response["success_rate"] = rates.get(key, response.get("success_rate", 0.3))
        responses.sort(key=lambda r: -float(r.get("success_rate", 0)))
        return responses[0] if responses else {"text": "", "style": "general"}

    # --------------------------------------------------------------- responses
    def context_values(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        agency = (self.agency() or {})
        info = agency.get("agency", {}) or {}
        packages = agency.get("packages", {}) or {}
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == lead.get("category")), {})
        trial = agency.get("trial_policy", {}) or {}
        terms = agency.get("payment_terms", {}) or {}

        issue_title = issues[0].get("title", "") if issues else "site mobile pe theek nahi khulti"
        issue_benefit = issues[0].get("benefit", "") if issues else "customers quietly chale jate hain"
        return {
            "business_name": lead.get("name", "aap ka business"),
            "owner_name": lead.get("owner_name") or "Sir",
            "city": lead.get("city", ""),
            "category": (lead.get("category", "") or "business").replace("_", " "),
            "issue": issue_title,
            "issue_benefit": issue_benefit,
            "issue_count": len(issues),
            "audit_score": lead.get("audit_score", 0),
            "basic_price": _fmt(packages.get("basic", {}).get("price", 15000)),
            "recommended_price": _fmt(packages.get("recommended", {}).get("price", 35000)),
            "premium_price": _fmt(packages.get("premium", {}).get("price", 75000)),
            "price": _fmt(packages.get(category.get("recommended_package", "recommended"),
                                       {}).get("price", 35000)),
            "monthly": _fmt(packages.get(category.get("recommended_package", "recommended"),
                                         {}).get("monthly", 3000)),
            "basic_monthly": _fmt(packages.get("basic", {}).get("monthly", 1500)),
            "delivery_days": packages.get(category.get("recommended_package", "recommended"),
                                          {}).get("delivery_days", 10),
            "advance_percent": int(str(terms.get("advance_percent", 50))),
            "my_name": info.get("owner_name", ""),
            "my_city": info.get("city", ""),
            "years": info.get("years_experience", 3),
            "portfolio_item": self._portfolio_line(category.get("id")),
            "portfolio_result": self._portfolio_result(category.get("id")),
            "scope": _scope_line(category, lead),
            "followup_days": 3,
            "proof_angle": category.get("proof_angle", ""),
            "refund_days": trial.get("refund_days", 7),
            "seconds": "9",
            "question": "Kya main aap ko 2 minute mein explain karoon?",
        }

    @staticmethod
    def _portfolio_line(category: Optional[str] = None) -> str:
        return _portfolio_item(category).get("name", "Karachi Biryani House")

    @staticmethod
    def _portfolio_result(category: Optional[str] = None) -> str:
        return _portfolio_item(category).get("result", "online orders 40% barh gaye")

    def render(self, template: str, values: Dict[str, Any]) -> str:
        def replace(match):
            key = match.group(1)
            return str(values.get(key, match.group(0)))
        text = re.sub(r"\{(\w+)\}", replace, template or "")
        return re.sub(r"\s+", " ", text).strip()

    def handle_objection(self, objection_text: str, lead: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
        """Main entry: detect the objection, answer it with proof."""
        lead = lead or {}
        objection_id, confidence = self.match_objection(objection_text)
        if not objection_id:
            # Nothing matched - do not guess. Ask, and offer the safest proof.
            return {
                "objection": None, "confidence": 0.0,
                "response": self.render(
                    "Samajh gaya hoon. Thoda aur detail dein to main theek jawab doon. "
                    "Itni baat pakki hai: main {business_name} ka free mockup bana kar "
                    "bhej deta hoon - aap khud dekh kar faisla karein, koi paisa nahi.",
                    self.context_values(lead)),
                "style": "trial", "success_rate": 0.3,
            }
        response = self.best_response(objection_id)
        values = self.context_values(lead)
        text = self.render(response.get("text", ""), values)
        text = self._attach_proof(text, objection_id, lead)
        return {
            "objection": objection_id, "confidence": confidence,
            "response": text, "style": response.get("style", "general"),
            "success_rate": response.get("success_rate", 0.3),
        }

    def _attach_proof(self, text: str, objection_id: str, lead: Dict[str, Any]) -> str:
        """Guarantee a specific proof point in every reply."""
        if _has_proof(text):
            return text
        payload = self.model()
        objection = next((o for o in payload.get("objections", [])
                          if o.get("id") == objection_id), {}) or {}
        proofs = objection.get("proof") or []
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        if issues:
            proof = "saboot: %s (%s)" % (issues[0].get("title", ""),
                                         issues[0].get("evidence", "") or
                                         "main ne abhi check kiya")
        elif proofs:
            proof = proofs[0]
        else:
            proof = "saboot: %s ke liye %s" % (lead.get("name", "aap jaisay business"),
                                               self._portfolio_result(lead.get("category")))
        return "%s\n\n(%s)" % (text, proof)

    # ------------------------------------------------------------ trust moves
    def offer_proof(self, lead: Dict[str, Any]) -> str:
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        if not issues:
            return ("Main aap ki site ka full audit report bhej deta hoon - "
                    "score, speed, aur mobile issues ke sath. Free hai.")
        top = issues[:2]
        lines = ["%s ki site ka audit (abhi kiya gaya):" % lead.get("name", "Aap ki site")]
        for index, issue in enumerate(top, 1):
            lines.append("  %d. %s - %s" % (index, issue.get("title", ""),
                                            issue.get("evidence", "")))
        lines.append("Score: %s/100" % lead.get("audit_score", 0))
        return "\n".join(lines)

    def offer_trial(self, lead: Dict[str, Any]) -> str:
        trial = (self.agency() or {}).get("trial_policy", {}) or {}
        values = self.context_values(lead)
        return self.render(
            "Aap ko decide karne se pehle dekhna chahiye. Main {business_name} ka free "
            "homepage mockup bana kar bhejta hoon - %s ghante mein. Aap ke asli content "
            "aur WhatsApp button ke sath. Pasand na aaye to koi paisa nahi, koi commitment nahi."
            % trial.get("mockup_hours", 24), values)

    def offer_monthly(self, lead: Dict[str, Any]) -> str:
        values = self.context_values(lead)
        return self.render(
            "Agar ek saath itna mushkil hai to monthly option hai: sirf {monthly} mahine "
            "+ chhoti setup fee. Upfront bohat kam, aur jab chahein rok sakte hain. "
            "Zyada tar {category} wale isi se shuru karte hain.", values)

    def build_trust_response(self, lead: Dict[str, Any],
                             situation: str = "who_are_you") -> str:
        result = self.handle_objection(situation, lead)
        return result["response"]

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        store = default_store()
        lead = None
        if context and context.get("lead_id"):
            lead = store.get_lead(int(context["lead_id"]))
        if lead is None:
            lowered = text.lower()
            for candidate in store.leads(limit=300):
                name = str(candidate.get("name", "")).lower()
                if name and name in lowered:
                    lead = candidate
                    break
        lead = lead or {}

        if any(word in text.lower() for word in ("proof", "audit", "saboot")):
            return self.offer_proof(lead)
        if any(word in text.lower() for word in ("trial", "mockup", "free demo")):
            return self.offer_trial(lead)
        if any(word in text.lower() for word in ("monthly", "mahine", "installment")):
            return self.offer_monthly(lead)

        result = self.handle_objection(text, lead)
        header = ""
        if result.get("objection"):
            header = "[objection: %s, confidence %.0f%%, template success %.0f%%]\n\n" % (
                result["objection"], result["confidence"] * 100,
                float(result.get("success_rate", 0)) * 100)
        return header + result["response"]

    def record_outcome(self, objection_id: str, style: str, won: bool) -> Dict[str, float]:
        payload = self.model()
        rates = dict(payload.get("success_rates") or {})
        key = "%s::%s" % (objection_id, style)
        current = float(rates.get(key, 0.5))
        rates[key] = round(max(0.02, min(0.98, current + (0.3 if won else -0.25) * 0.4)), 4)
        payload["success_rates"] = rates
        self.save_model(self.MODEL, payload)
        return rates


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _fmt(amount: Any) -> str:
    """35000 -> '35,000' (Pakistani numbering reads better with commas)."""
    try:
        return "{:,}".format(int(amount))
    except Exception:
        return str(amount)


def _portfolio_item(category: Optional[str] = None) -> Dict[str, Any]:
    from ... import FACTORY_ROOT
    import json as _json
    path = FACTORY_ROOT / "config" / "agency.json"
    try:
        agency = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        agency = {}
    items = agency.get("portfolio", []) or []
    if category:
        matches = [i for i in items if i.get("category") == category]
        if matches:
            return matches[0]
    return (items or [{}])[0]


def _scope_line(category: Dict[str, Any], lead: Dict[str, Any]) -> str:
    package = category.get("recommended_package", "recommended")
    pages = {"basic": "5 pages", "recommended": "10 pages", "premium": "unlimited pages"}
    features = {"basic": "mobile design, WhatsApp button, contact form, Google Map",
                "recommended": "custom design, SEO, speed optimisation, Google Business, analytics",
                "premium": "booking/e-commerce engine, payment gateway, admin panel, copywriting"}
    return "%s - %s + %s" % (package, pages.get(package, "10 pages"),
                             features.get(package, "mobile design"))


#: Phrases that count as a concrete proof point.
PROOF_MARKERS = ("saboot", "audit", "score", "result", "%", "second", "ms", "PKR",
                 "mockup", "commission", "orders", "appointments", "portfolio")


def _has_proof(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in PROOF_MARKERS)
