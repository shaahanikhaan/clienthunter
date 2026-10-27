"""apt_auditor - the proof engine.

This is the apartment that makes outreach work.  A cold message that opens with
"we build websites" gets ignored; a message that opens with "your site takes 9.4
seconds to load on mobile, which is why people bounce" gets a reply.

It audits SSL, speed, mobile-friendliness, SEO basics and broken links with
nothing but the standard library, then writes a Roman-Urdu report that
apt_outreach quotes almost verbatim.

Learning: ``models/impact.json`` holds a weight per issue type.  apt_self_trainer
calls :meth:`record_outcome` whenever a lead converts, so the report learns to
lead with the issues that actually win clients in your market.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment


class AuditorAgent(BaseApartment):
    NAME = "auditor"
    DESCRIPTION = "Audits client websites (SSL, speed, mobile, SEO, links) and writes proof reports"
    PERMISSIONS = {"internet": True, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "impact.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        """Seed impact weights from the issue library + category playbooks."""
        from ..tools.audit import ISSUE_LIBRARY

        learned = self.load_model(self.MODEL, default={}) or {}
        weights = dict(learned.get("weights") or {})

        # Category playbooks tell us which issues matter for which business type.
        categories = (self.load_shared_data("business_categories.json", default={}) or {}
                      ).get("categories", [])
        for category in categories:
            for issue in category.get("typical_issues", []):
                if issue in ISSUE_LIBRARY and issue not in weights:
                    weights[issue] = float(ISSUE_LIBRARY[issue]["weight"])

        for issue, meta in ISSUE_LIBRARY.items():
            weights.setdefault(issue, float(meta["weight"]))

        model = {
            "weights": weights,
            "samples": int(learned.get("samples", 0)),
            "updated_at": time.time(),
            "notes": "Per-issue impact weight (1-15). Higher = leads the report and the "
                     "outreach opener. Updated from real won/lost outcomes.",
        }
        self.save_model(self.MODEL, model)
        self.log("trained", issues=len(weights))
        return {"issues": len(weights), "samples": model["samples"]}

    def weights(self) -> Dict[str, float]:
        model = self.load_model(self.MODEL, default={}) or {}
        return model.get("weights") or {}

    # ------------------------------------------------------------------ audit
    def audit_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Audit one lead, save the report to the sandbox, update the DB."""
        tool = self.tool("audit")
        website = lead.get("website") or ""
        if not website:
            result = {"url": "", "score": 0, "issues": [
                {"id": "no_website", "title": "Koi website nahi hai",
                 "benefit": "Google Maps pe aap ka business hai par details nahi miltin",
                 "weight": self.weights().get("no_website", 10),
                 "evidence": "website field khaali hai"}],
                "details": {}, "report": ""}
            result["report"] = tool.build_report(lead, result["issues"], 0)
        else:
            result = tool.audit(website)

        if lead.get("id"):
            store = default_store()
            store.update_fields(
                int(lead["id"]),
                audit_score=int(result.get("score", -1)),
                audit_issues=json.dumps(result.get("issues", [])[:8]),
                site_status=(result.get("details", {}).get("ssl") or {}).get("ok")
                and "live" or result.get("site_status", "live"),
            )
            store.log_event("auditor", "lead_audited",
                            {"lead_id": lead["id"], "score": result.get("score"),
                             "issues": len(result.get("issues", []))})

        if lead.get("id"):
            safe_name = "".join(ch if ch.isalnum() else "_"
                                for ch in str(lead.get("name", "lead")))[:40]
            self.write_text("reports/lead_%s_%s.txt" % (lead["id"], safe_name),
                            result.get("report", ""))
        return result

    def top_issue(self, lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The single issue to open with - used verbatim by outreach."""
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        if not issues and lead.get("website"):
            issues = self.audit_lead(lead).get("issues", [])
        return issues[0] if issues else None

    def audit_batch(self, limit: int = 10) -> List[Dict[str, Any]]:
        store = default_store()
        leads = [lead for lead in store.leads(limit=300)
                 if lead.get("website") and (lead.get("audit_score") in (-1, None, 0))]
        if not leads:
            leads = [lead for lead in store.leads(limit=300) if lead.get("website")]
        results = []
        for lead in leads[:limit]:
            if self.kill_switch():
                break
            if self.offline:
                break
            try:
                results.append(self.audit_lead(lead))
            except Exception as exc:
                self.log("audit_failed", lead=lead.get("name"), error=str(exc))
        return results

    def record_outcome(self, lead: Dict[str, Any], converted: bool) -> Dict[str, float]:
        issues = lead.get("audit_issues") or []
        if isinstance(issues, str):
            try:
                issues = json.loads(issues)
            except Exception:
                issues = []
        return self.tool("audit").record_outcome(issues, converted)

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").lower()
        store = default_store()
        target = None
        for lead in store.leads(limit=200):
            name = str(lead.get("name", "")).lower()
            if name and name in text:
                target = lead
                break
        if target is None and context and context.get("lead_id"):
            target = store.get_lead(int(context["lead_id"]))

        if target is not None:
            if self.offline:
                issues = target.get("audit_issues") or []
                if not issues:
                    return ("%s ki site offline mode mein audit nahi ho sakti. "
                            "Internet on kar ke dobara chalaayein." % target.get("name"))
                return self.tool("audit").build_report(target, issues,
                                                       target.get("audit_score", 0))
            result = self.audit_lead(target)
            return result.get("report") or "Audit complete: koi bara issue nahi mila."

        if self.offline:
            rows = [lead for lead in store.leads(limit=200) if lead.get("audit_issues")]
            if rows:
                return "\n\n".join(
                    "%s (score %s):\n%s" % (lead.get("name"), lead.get("audit_score"),
                                            self.tool("audit").build_report(
                                                lead, lead.get("audit_issues") or [],
                                                lead.get("audit_score", 0)))
                    for lead in rows[:5])
            return "Offline mode: audit ke liye internet chahiye."

        results = self.audit_batch(limit=5)
        if not results:
            return "Audit ke liye koi lead nahi mila (pehle hunter chalayein)."
        return "\n\n".join("%s\n%s" % (result.get("url", ""), result.get("report", ""))
                           for result in results)

    def run(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.audit_batch(limit=limit)
