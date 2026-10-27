"""apt_requirements - turns a conversation into a structured brief.

Progressive questioning: it asks the single most informative missing question
next (not a form dump), stores answers, and produces ``clients/{id}/brief.json``.

The question bank comes from ``data/business_categories.json`` (each category
carries its own discovery questions) plus a universal set.  The order is learned:
``models/question_templates.json`` tracks which questions, once answered, were
present in won deals.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

#: Universal discovery questions, in the order they usually matter.
UNIVERSAL_QUESTIONS = [
    {"field": "business_name", "question": "Aap ke business ka poora naam kya hai?",
     "why": "Site ka title aur branding isi se tay hoti hai"},
    {"field": "category", "question": "Aap ka business kis category mein aata hai (restaurant, clinic, salon...)?",
     "why": "Design aur features category ke hisaab se hote hain"},
    {"field": "target_audience", "question": "Aap ke customers kaun hain - local family, students, corporates?",
     "why": "Language aur tone isi se tay hoti hai"},
    {"field": "current_pain", "question": "Abhi sab se bari pareshani kya hai? (orders nahi aate, calls zyada, etc.)",
     "why": "Ye hi wo cheez hai jo site ko solve karni hai"},
    {"field": "budget_range", "question": "Is ke liye aap ka budget kitna hai (range bata dein)?",
     "why": "Package isi se recommend hota hai"},
    {"field": "timeline", "question": "Kab tak live chahiye?",
     "why": "Delivery plan isi se banta hai"},
    {"field": "decision_maker", "question": "Final decision aap khud karte hain ya koi aur?",
     "why": "Agar partner/board hai to un ke liye one-pager banate hain"},
    {"field": "competitors", "question": "Aap ke competitors kaun hain - kisi ki site pasand aayi ho to bata dein?",
     "why": "Benchmark mil jata hai"},
    {"field": "content_ready", "question": "Text aur photos aap ke paas ready hain ya hum likh dein?",
     "why": "Content writing ka scope isi se tay hota hai"},
    {"field": "features", "question": "Kaun se features lazmi hain? (WhatsApp button, booking, payment, gallery)",
     "why": "Scope lock karne ke liye"},
]

#: Which fields absolutely must exist before a brief is useful.
REQUIRED_FIELDS = ("business_name", "category", "current_pain", "budget_range",
                   "timeline", "features")


class RequirementsAgent(BaseApartment):
    NAME = "requirements"
    DESCRIPTION = "Progressive discovery - turns a conversation into a structured JSON brief"
    PERMISSIONS = {"internet": False, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "question_templates.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        learned = self.load_model(self.MODEL, default={}) or {}
        order = learned.get("order") or [q["field"] for q in UNIVERSAL_QUESTIONS]

        per_category: Dict[str, List[Dict[str, str]]] = {}
        for category in categories:
            cid = category.get("id")
            if not cid:
                continue
            questions = [{"field": "pain_%s" % cid, "question": q, "why": "category-specific"}
                         for q in category.get("questions", [])]
            pain_points = [{"field": "pain_point", "question": "%s - ye masla aap ko bhi hai?"
                            % pain, "why": "category pain point"}
                           for pain in category.get("pain_points", [])[:2]]
            per_category[cid] = questions + pain_points

        model = {
            "universal": UNIVERSAL_QUESTIONS,
            "per_category": per_category,
            "order": order,
            "required": list(REQUIRED_FIELDS),
            "answer_stats": learned.get("answer_stats", {}),
            "notes": "'order' is relearned by apt_self_trainer from briefs that ended in won deals.",
        }
        self.save_model(self.MODEL, model)
        self.log("trained", categories=len(per_category),
                 universal=len(UNIVERSAL_QUESTIONS))
        return {"universal": len(UNIVERSAL_QUESTIONS), "categories": len(per_category)}

    def bank(self) -> Dict[str, Any]:
        return self.load_model(self.MODEL, default={}) or {}

    # -------------------------------------------------------------- brief i/o
    def _brief_path(self, lead_id: int) -> str:
        return "clients/%s/brief.json" % lead_id

    def new_client(self, lead_id: int, lead: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        store = default_store()
        lead = lead or store.get_lead(int(lead_id)) or {}
        brief = {
            "lead_id": int(lead_id),
            "created_at": time.time(),
            "updated_at": time.time(),
            "answers": {
                "business_name": lead.get("name", ""),
                "category": lead.get("category", ""),
                "city": lead.get("city", ""),
                "phone": lead.get("phone", ""),
                "email": lead.get("email", ""),
                "website": lead.get("website", ""),
            },
            "asked": [],
            "complete": False,
        }
        self.write_json(self._brief_path(int(lead_id)), brief)
        self.db.put_item("brief:%s" % lead_id, brief)
        self.log("brief_created", lead_id=lead_id)
        return brief

    def get_brief(self, lead_id: int) -> Dict[str, Any]:
        brief = self.read_json(self._brief_path(int(lead_id)), default=None)
        if brief is None:
            brief = self.db.get_item("brief:%s" % lead_id)
        return brief or {}

    def save_answer(self, lead_id: int, field: str, value: Any) -> Dict[str, Any]:
        brief = self.get_brief(int(lead_id)) or self.new_client(int(lead_id))
        brief.setdefault("answers", {})[field] = value
        brief["updated_at"] = time.time()
        brief["complete"] = not self.missing_fields(brief)
        self.write_json(self._brief_path(int(lead_id)), brief)
        self.db.put_item("brief:%s" % lead_id, brief)
        self.log("answer_saved", lead_id=lead_id, field=field)
        return brief

    # --------------------------------------------------------------- questions
    def questions_for(self, lead: Dict[str, Any]) -> List[Dict[str, str]]:
        bank = self.bank()
        category = lead.get("category", "")
        questions = list(bank.get("universal") or UNIVERSAL_QUESTIONS)
        questions += (bank.get("per_category", {}) or {}).get(category, [])
        order = bank.get("order") or []
        questions.sort(key=lambda q: order.index(q["field"])
                       if q["field"] in order else 99)
        return questions

    def missing_fields(self, brief: Dict[str, Any]) -> List[str]:
        answers = brief.get("answers", {})
        return [field for field in REQUIRED_FIELDS
                if not str(answers.get(field, "")).strip()]

    def next_question(self, lead_id: int) -> Optional[Dict[str, str]]:
        """The single most informative question still unanswered."""
        store = default_store()
        lead = store.get_lead(int(lead_id)) or {}
        brief = self.get_brief(int(lead_id))
        if not brief:
            brief = self.new_client(int(lead_id), lead)
        answers = brief.get("answers", {})
        for question in self.questions_for(lead):
            if question["field"] in (brief.get("asked") or []):
                continue
            if str(answers.get(question["field"], "")).strip():
                continue
            question = dict(question)
            question["lead_id"] = int(lead_id)
            return question
        return None

    def ask_question(self, lead_id: int) -> str:
        question = self.next_question(int(lead_id))
        if not question:
            return "Brief complete hai. generate_brief() chalaayein."
        brief = self.get_brief(int(lead_id)) or {}
        brief.setdefault("asked", []).append(question["field"])
        self.write_json(self._brief_path(int(lead_id)), brief)
        self.db.put_item("brief:%s" % lead_id, brief)
        self.log("question_asked", lead_id=lead_id, field=question["field"])
        return question.get("question", "")

    def record_answer_from_text(self, lead_id: int, text: str) -> Dict[str, Any]:
        """Parse a free-text answer into the field we just asked about."""
        brief = self.get_brief(int(lead_id)) or {}
        asked = brief.get("asked") or []
        field = asked[-1] if asked else "notes"
        if field.startswith("pain_"):
            field = "current_pain"
        value = (text or "").strip()
        entities = self.tool("nlp_urdu").extract_entities(value)
        if field == "budget_range" and entities.get("prices"):
            value = "%s PKR" % entities["prices"][0]["value"]
        if field == "timeline" and entities.get("dates"):
            value = str(entities["dates"][0].get("raw", value))
        return self.save_answer(int(lead_id), field, value)

    # ------------------------------------------------------------------ brief
    def generate_brief(self, lead_id: int) -> Dict[str, Any]:
        store = default_store()
        lead = store.get_lead(int(lead_id)) or {}
        brief = self.get_brief(int(lead_id)) or {}
        answers = brief.get("answers", {})
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        category = next((c for c in categories if c.get("id") == answers.get("category")), {})

        budget = _parse_budget(str(answers.get("budget_range", "")))
        recommended = _recommend_package(budget, category)

        document = {
            "lead_id": int(lead_id),
            "business_name": answers.get("business_name") or lead.get("name", ""),
            "category": answers.get("category", ""),
            "city": answers.get("city", ""),
            "contact": {"phone": lead.get("phone", ""), "email": lead.get("email", ""),
                        "website": lead.get("website", "")},
            "target_audience": answers.get("target_audience", ""),
            "current_pain": answers.get("current_pain", ""),
            "pain_points": [v for k, v in answers.items() if k.startswith("pain")],
            "budget_range": answers.get("budget_range", ""),
            "budget_pkr": budget,
            "timeline": answers.get("timeline", ""),
            "decision_maker": answers.get("decision_maker", ""),
            "competitors": answers.get("competitors", ""),
            "content_ready": answers.get("content_ready", ""),
            "features": _as_list(answers.get("features", "")),
            "recommended_package": recommended,
            "category_playbook": {
                "proof_angle": category.get("proof_angle", ""),
                "typical_issues": category.get("typical_issues", []),
                "likely_objections": category.get("objections_likely", []),
            },
            "missing": self.missing_fields(brief),
            "audit": {"score": lead.get("audit_score"), "issues": lead.get("audit_issues", [])},
            "generated_at": time.time(),
        }
        self.write_json("clients/%s/brief_final.json" % lead_id, document)
        self.db.put_item("brief_final:%s" % lead_id, document)
        if lead.get("id"):
            store.set_stage(int(lead_id), "qualified")
            store.log_event("requirements", "brief_generated",
                            {"lead_id": lead_id, "package": recommended})
        self.log("brief_generated", lead_id=lead_id, package=recommended)
        return document

    def brief_summary(self, lead_id: int) -> str:
        brief = self.db.get_item("brief_final:%s" % lead_id) or self.generate_brief(int(lead_id))
        lines = ["Brief for #%s - %s (%s, %s)" % (lead_id, brief.get("business_name"),
                                                  brief.get("category"), brief.get("city"))]
        for label, key in (("Target audience", "target_audience"),
                           ("Pain", "current_pain"),
                           ("Budget", "budget_range"),
                           ("Timeline", "timeline"),
                           ("Decision maker", "decision_maker"),
                           ("Competitors", "competitors"),
                           ("Content", "content_ready")):
            value = brief.get(key)
            if value:
                lines.append("  %-15s: %s" % (label, value))
        if brief.get("features"):
            lines.append("  %-15s: %s" % ("Features", ", ".join(brief["features"])))
        lines.append("  %-15s: %s" % ("Recommended", brief.get("recommended_package")))
        if brief.get("missing"):
            lines.append("  Missing         : %s" % ", ".join(brief["missing"]))
        return "\n".join(lines)

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        store = default_store()
        lead_id = (context or {}).get("lead_id")

        if lead_id is None:
            for lead in store.leads(limit=300):
                name = str(lead.get("name", "")).lower()
                if name and name in lowered:
                    lead_id = lead["id"]
                    break
        if lead_id is None:
            import re
            match = re.search(r"(?:client_|lead_|#)(\d+)", lowered)
            if match:
                lead_id = int(match.group(1))

        if any(word in lowered for word in ("brief", "summary", "dikhao")) and lead_id:
            return self.brief_summary(int(lead_id))

        if lead_id is None:
            leads = store.leads(stage="replied", limit=10) or store.leads(limit=10)
            if not leads:
                return "Koi lead nahi mila. Pehle hunter chalayein."
            lead_id = leads[0]["id"]

        if any(word in lowered for word in ("sawal", "question", "poocho", "ask")):
            return self.ask_question(int(lead_id))

        # Anything else is treated as the answer to the last question.
        if text and not any(word in lowered for word in ("requirements", "brief banao",
                                                         "generate")):
            brief = self.get_brief(int(lead_id))
            if brief and (brief.get("asked") or []):
                self.record_answer_from_text(int(lead_id), text)
                nxt = self.next_question(int(lead_id))
                if nxt:
                    return "Saved. Agla sawal: %s" % nxt["question"]
                return ("Brief complete. %s" % self.brief_summary(int(lead_id)))

        return self.ask_question(int(lead_id))

    def run(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Advance discovery for leads that replied but have no brief yet."""
        store = default_store()
        leads = store.leads(stage="replied", limit=limit) or []
        out = []
        for lead in leads:
            if self.kill_switch():
                break
            question = self.next_question(int(lead["id"]))
            if question:
                out.append({"lead": lead.get("name"), "next_question": question["question"]})
        return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _as_list(value: Any) -> List[str]:
    """Split a free-text feature answer into clean items."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "")
    parts = [part.strip(" .") for part in re.split(r"[,;]|aur|\band\b", text)]
    return [p for p in parts if len(p) > 2][:10]


def _parse_budget(text: str) -> Optional[int]:
    """Pull a number out of '30 hazar se 40 hazar tak' / '50k' / '40000'."""
    digits = re.sub(r"[^0-9]", " ", text or "")
    numbers = [int(n) for n in digits.split() if n.isdigit() and int(n) > 500]
    if not numbers:
        return None
    return min(numbers) if len(numbers) > 1 else numbers[0]


def _recommend_package(budget: Optional[int], category: Dict[str, Any]) -> str:
    """Map budget + category to Basic / Recommended / Premium."""
    typical = int(category.get("avg_budget_pkr", 35000) or 35000)
    if budget is None:
        return "recommended"
    if budget < 20000:
        return "basic"
    if budget < 60000:
        return "basic" if typical < 25000 else "recommended"
    return "premium"
