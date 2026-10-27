"""apt_closer - pricing, quotes, trials and deal closing.

Two jobs:

1. **Qualify** - a logistic regression trained from scratch (gradient descent,
   pure Python) on ``data/leads_history.json`` scores every lead on how likely it
   is to become a paying client, so the closer spends its day on winnable deals.
2. **Close** - produce a 3-option quote (Basic / Recommended / Premium) that
   anchors high first, always paired with either a 1-month trial or a
   month-to-month plan, and never promises anything the agency cannot deliver.

Rule enforced in code: no quote is ever sent without a proof point and an
explicit next step.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from ...ml import LogisticRegression, train_test_split
from ...store import default_store
from ..base_apartment import BaseApartment

FEATURES = ["has_phone", "has_email", "has_website", "rating", "reviews", "responded",
            "response_hours", "business_size", "budget_signal", "timeline_signal",
            "decision_maker", "category_weight"]


class CloserAgent(BaseApartment):
    NAME = "closer"
    DESCRIPTION = "Lead qualification (on-device logistic regression), quotes, trials, invoices"
    PERMISSIONS = {"email": True, "internet": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "qualifier.json"
    PRICING = "pricing.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        data = self.load_shared_data("leads_history.json", default={}) or {}
        examples = data.get("examples", [])
        features = data.get("features") or FEATURES
        label = data.get("label", "qualified")
        if len(examples) < 20:
            raise ValueError("need >= 20 historical leads, got %d" % len(examples))

        rows = [([float(example.get(f, 0) or 0) for f in features],
                 int(example.get(label, 0))) for example in examples]
        X = [row[0] for row in rows]
        y = [row[1] for row in rows]

        train, test = train_test_split(list(zip(X, y)), ratio=0.8, seed=21)
        model = LogisticRegression(learning_rate=0.25, epochs=700, l2=1e-3).fit(
            [item[0] for item in train], [item[1] for item in train],
            feature_names=features)
        accuracy = model.score([item[0] for item in test],
                               [item[1] for item in test]) if test else 0.0

        self.save_model(self.MODEL, model.to_dict())
        self.save_model(self.PRICING, self._pricing_snapshot())
        stats = {
            "examples": len(examples),
            "train_size": len(train),
            "test_size": len(test),
            "holdout_accuracy": round(accuracy, 3),
            "top_features": [{"feature": f, "weight": round(w, 3)}
                             for f, w in model.top_features(5)],
            "final_loss": model.loss_history[-1] if model.loss_history else None,
        }
        self.remember("train_stats", stats)
        self.log("trained", **{k: v for k, v in stats.items() if not isinstance(v, list)})
        return stats

    def _pricing_snapshot(self) -> Dict[str, Any]:
        agency = self.agency() or {}
        return {
            "packages": agency.get("packages", {}),
            "addons": agency.get("addons", []),
            "trial_policy": agency.get("trial_policy", {}),
            "payment_terms": agency.get("payment_terms", {}),
            "guarantees": agency.get("guarantees", []),
            "currency": agency.get("currency", "PKR"),
        }

    def pricing(self) -> Dict[str, Any]:
        return self.load_model(self.PRICING, default=None) or self._pricing_snapshot()

    # -------------------------------------------------------------- qualifying
    def qualifier(self) -> Optional[LogisticRegression]:
        data = self.load_model(self.MODEL, default={})
        if not data:
            return None
        try:
            return LogisticRegression.from_dict(data)
        except Exception:
            return None

    def feature_vector(self, lead: Dict[str, Any],
                       brief: Optional[Dict[str, Any]] = None) -> List[float]:
        brief = brief or {}
        answers = brief.get("answers", {}) if isinstance(brief, dict) else {}
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == lead.get("category")), {})
        store = default_store()
        messages = store.messages(int(lead["id"])) if lead.get("id") else []
        inbound = [m for m in messages if m.get("direction") == "in"]
        responded = 1 if inbound else (1 if lead.get("stage") not in ("new", None) else 0)
        response_hours = 24.0
        if inbound:
            first_in = min((m.get("created_at") or 0) for m in inbound)
            out_before = [m for m in messages
                          if m.get("direction") == "out" and (m.get("created_at") or 0) <= first_in]
            if out_before:
                response_hours = max(0.5, (first_in - max(m["created_at"] for m in out_before)) / 3600.0)
        budget_signal = 1 if str(answers.get("budget_range", "")).strip() else 0
        timeline = str(answers.get("timeline", "")).lower()
        timeline_signal = 1 if any(word in timeline for word in
                                   ("din", "day", "week", "hafte", "jaldi", "urgent")) else 0
        decision = str(answers.get("decision_maker", "")).lower()
        decision_maker = 1 if any(word in decision for word in
                                  ("main", "khud", "me", "i ", "self")) else 0
        return [
            1.0 if lead.get("phone") else 0.0,
            1.0 if lead.get("email") else 0.0,
            1.0 if lead.get("website") else 0.0,
            float(lead.get("rating") or 0),
            float(lead.get("reviews") or 0),
            float(responded),
            float(response_hours),
            float(answers.get("business_size") or 1),
            float(budget_signal),
            float(timeline_signal),
            float(decision_maker),
            float(category.get("conversion_weight", 1.0)),
        ]

    def qualify(self, lead: Dict[str, Any],
                brief: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        model = self.qualifier()
        vector = self.feature_vector(lead, brief)
        if model is None:
            return {"probability": 0.5, "qualified": None, "untrained": True,
                    "features": dict(zip(FEATURES, vector))}
        probability = model.predict_proba(vector)
        contributions = sorted(zip(FEATURES, model.weights), key=lambda kv: -abs(kv[1]))[:4]
        return {
            "probability": round(probability, 3),
            "qualified": int(probability >= 0.5),
            "top_factors": [{"feature": f, "weight": round(w, 3)} for f, w in contributions],
            "features": dict(zip(FEATURES, vector)),
        }

    def rank_leads(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Best leads to work today: qualified probability x deal size."""
        store = default_store()
        out = []
        for lead in store.leads(limit=200):
            if lead.get("stage") in ("won", "lost"):
                continue
            result = self.qualify(lead)
            categories = (self.load_shared_data("business_categories.json", default={}) or {}
                          ).get("categories", [])
            category = next((c for c in categories if c.get("id") == lead.get("category")), {})
            expected = result["probability"] * float(category.get("avg_budget_pkr", 25000))
            out.append({"lead": lead, "probability": result["probability"],
                        "expected_value_pkr": round(expected),
                        "top_factors": result.get("top_factors", [])})
        out.sort(key=lambda item: -item["expected_value_pkr"])
        return out[:limit]

    # ----------------------------------------------------------------- quotes
    def quote(self, lead: Dict[str, Any], package: Optional[str] = None,
              extras: Optional[List[str]] = None) -> str:
        """3-option quote.  Anchors on Premium, recommends the middle."""
        pricing = self.pricing()
        packages = pricing.get("packages", {})
        currency = pricing.get("currency", "PKR")
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == lead.get("category")), {})
        package = package or category.get("recommended_package", "recommended")
        recommended = packages.get(package, packages.get("recommended", {}))

        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        # Never invent a finding: if the audit has not run, say so and offer it.
        if issues:
            first = issues[0]
            proof = "%s - %s" % (first.get("title"), first.get("benefit"))
            proof_line = "Jo masla hum solve karenge: %s (audit score %s/100)" % (
                proof, lead.get("audit_score", 0))
        else:
            proof_line = ("Audit abhi nahi hua - pehle free audit kar lete hain, "
                          "us ki findings ke hisaab se quote final kar denge.")

        lines = ["%s ke liye quotation:" % lead.get("name", "Aap ka business")]
        lines.append("")
        lines.append(proof_line)
        lines.append("")
        option_no = 0
        for key in ("premium", "recommended", "basic"):     # anchor high first
            data = packages.get(key, {})
            if not data:
                continue
            option_no += 1
            marker = " <-- recommended" if key == package else ""
            lines.append("%s. %s - %s %s (ya %s/mah) | %s din | %s%s" % (
                option_no, data.get("name", key),
                _fmt(data.get("price", 0)), currency, _fmt(data.get("monthly", 0)),
                data.get("delivery_days", "-"),
                ", ".join(data.get("includes", [])[:4]), marker))

        addons = pricing.get("addons", []) or []
        if extras and addons:
            chosen = [a for a in addons if a.get("id") in extras]
            if chosen:
                lines.append("")
                lines.append("Add-ons: " + ", ".join(
                    "%s %s %s" % (a.get("name"), _fmt(a.get("price")), currency)
                    for a in chosen))

        trial = pricing.get("trial_policy", {}) or {}
        terms = pricing.get("payment_terms", {}) or {}
        lines.append("")
        lines.append("Shuruat ka tareeqa (jo asaan lage):")
        lines.append("  a) Free mockup 24 ghante mein - pasand aaye tab aage (koi paisa nahi)")
        lines.append("  b) %s%% advance, baqi delivery par" % terms.get("advance_percent", 50))
        lines.append("  c) Sirf %s %s mahine - monthly plan" % (
            _fmt(recommended.get("monthly", 0)), currency))
        lines.append("")
        lines.append("Guarantee: %s" % "; ".join((pricing.get("guarantees") or [])[:3]))
        lines.append("")
        lines.append("Agla qadam: mockup approve karein to main kal start kar deta hoon, "
                     "%s din mein live." % recommended.get("delivery_days", 10))
        return "\n".join(lines)

    def offer_trial(self, lead: Dict[str, Any]) -> str:
        trial = self.pricing().get("trial_policy", {}) or {}
        return ("Free trial: main %s ka homepage mockup %s ghante mein bana kar bhejta hoon. "
                "Aap ke asli content, WhatsApp button aur mobile layout ke sath. "
                "Pasand na aaye to koi paisa nahi - %s din ki money-back guarantee har "
                "project ke sath hai." % (lead.get("name", "aap ka business"),
                                          trial.get("mockup_hours", 24),
                                          trial.get("refund_days", 7)))

    def offer_monthly(self, lead: Dict[str, Any]) -> str:
        packages = self.pricing().get("packages", {})
        currency = self.pricing().get("currency", "PKR")
        lines = ["Monthly option (koi bara upfront nahi):"]
        for key in ("basic", "recommended", "premium"):
            data = packages.get(key, {})
            if data:
                lines.append("  %s: %s %s mahine + chhoti setup fee" % (
                    data.get("name", key), _fmt(data.get("monthly", 0)), currency))
        return "\n".join(lines)

    def send_invoice(self, lead: Dict[str, Any], package: str = "recommended",
                     dry_run: Optional[bool] = None) -> Dict[str, Any]:
        packages = self.pricing().get("packages", {})
        data = packages.get(package, packages.get("recommended", {}))
        price = int(data.get("price", 0) or 0)
        advance = int(round(price * (self.pricing().get("payment_terms", {})
                                     .get("advance_percent", 50) / 100.0)))
        body = self._invoice_text(lead, data, price, advance)
        if not lead.get("email"):
            return {"ok": False, "reason": "lead has no email address"}
        result = self.tool("email").send(
            lead["email"], "Invoice / advance - %s" % lead.get("name", "project"), body,
            dry_run=dry_run)
        if result.get("ok") and lead.get("id"):
            store = default_store()
            store.log_message(int(lead["id"]), "out", "email", body, intent="invoice")
            store.log_event("closer", "invoice_sent", {"lead_id": lead["id"],
                                                       "package": package, "advance": advance})
        return result

    def _invoice_text(self, lead: Dict[str, Any], package: Dict[str, Any],
                      price: int, advance: int) -> str:
        currency = self.pricing().get("currency", "PKR")
        terms = self.pricing().get("payment_terms", {}) or {}
        return "\n".join([
            "Invoice - %s" % lead.get("name", ""),
            "",
            "Package : %s" % package.get("name", ""),
            "Total   : %s %s" % (_fmt(price), currency),
            "Advance : %s %s (%s%%)" % (_fmt(advance), currency,
                                        terms.get("advance_percent", 50)),
            "Balance : %s %s on delivery" % (_fmt(price - advance), currency),
            "Delivery: %s din" % package.get("delivery_days", "-"),
            "",
            "Payment: " + ", ".join(terms.get("methods", ["bank transfer"])),
            "Guarantee: " + "; ".join((self.pricing().get("guarantees") or [])[:2]),
            "",
            "Advance milte hi kaam start ho jayega. Shukriya!",
        ])

    def schedule_call(self, lead: Dict[str, Any], when: str = "") -> str:
        store = default_store()
        when = when or "kal shaam 6 baje"
        if lead.get("id"):
            store.log_event("closer", "call_scheduled",
                            {"lead_id": lead["id"], "when": when})
        return ("Call schedule: %s ke sath %s. Main 5 minute pehle reminder bhej dunga. "
                "Agar ye waqt theek na ho to bata dein." % (lead.get("name", "client"), when))

    def mark_won(self, lead_id: int, value_pkr: int) -> None:
        store = default_store()
        store.set_stage(int(lead_id), "won", score=100)
        store.update_fields(int(lead_id), notes="won: %s PKR" % value_pkr)
        store.log_event("closer", "deal_won", {"lead_id": lead_id, "value_pkr": value_pkr})
        self.log("deal_won", lead_id=lead_id, value=value_pkr)

    def mark_lost(self, lead_id: int, reason: str = "") -> None:
        store = default_store()
        store.set_stage(int(lead_id), "lost")
        store.update_fields(int(lead_id), notes="lost: %s" % reason[:120])
        store.log_event("closer", "deal_lost", {"lead_id": lead_id, "reason": reason[:120]})

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        store = default_store()

        lead = None
        if context and context.get("lead_id"):
            lead = store.get_lead(int(context["lead_id"]))
        if lead is None:
            for candidate in store.leads(limit=300):
                name = str(candidate.get("name", "")).lower()
                if name and name in lowered:
                    lead = candidate
                    break
        if lead is None:
            ranked = self.rank_leads(limit=1)
            lead = ranked[0]["lead"] if ranked else None
        if lead is None:
            return "Koi lead nahi mila. Pehle hunter aur outreach chalayein."

        if any(word in lowered for word in ("rank", "best", "kaun", "priority", "qualify")):
            ranked = self.rank_leads(limit=8)
            return "\n".join("%s - %.0f%% chance, expected %s PKR" % (
                item["lead"].get("name"), item["probability"] * 100,
                _fmt(item["expected_value_pkr"])) for item in ranked)
        if any(word in lowered for word in ("trial", "mockup")):
            return self.offer_trial(lead)
        if any(word in lowered for word in ("monthly", "mahine")):
            return self.offer_monthly(lead)
        if any(word in lowered for word in ("invoice", "bill")):
            result = self.send_invoice(lead)
            return ("Invoice bhej diya." if result.get("ok")
                    else "Invoice nahi bhej saka: %s" % result.get("reason"))
        if any(word in lowered for word in ("call", "meeting")):
            return self.schedule_call(lead)

        qualification = self.qualify(lead)
        header = "Qualification: %.0f%% chance to close" % (qualification["probability"] * 100)
        if qualification.get("top_factors"):
            header += " (top factors: %s)" % ", ".join(
                f["feature"] for f in qualification["top_factors"][:3])
        return "%s\n\n%s" % (header, self.quote(lead))

    def run(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Daily: send quotes to the best-qualified leads that replied."""
        store = default_store()
        out = []
        for item in self.rank_leads(limit=limit):
            lead = item["lead"]
            if self.kill_switch():
                break
            if lead.get("stage") in ("new",):
                continue
            quote = self.quote(lead)
            if lead.get("email"):
                result = self.tool("email").send(
                    lead["email"], "Quotation - %s" % lead.get("name", "project"), quote)
                out.append({"lead": lead.get("name"), "sent": bool(result.get("ok")),
                            "probability": item["probability"]})
                if result.get("ok"):
                    store.log_message(int(lead["id"]), "out", "email", quote,
                                      intent="quote")
            else:
                out.append({"lead": lead.get("name"), "sent": False,
                            "reason": "no email", "probability": item["probability"]})
        return out


def _fmt(amount: Any) -> str:
    try:
        return "{:,}".format(int(amount))
    except Exception:
        return str(amount)
