"""apt_knowledge - the agency brain.

Trains a TF-IDF index over ``data/knowledge.json`` (pricing, packages, FAQ,
scripts, portfolio, guarantees) and serves it to the rest of the factory.

Isolation note: other apartments cannot read this one's files.  They each call
:meth:`context` through the orchestrator, or train their own tiny index from the
shared ``data/knowledge.json``.  That is deliberate - it means no agent can be
poisoned by another agent's state.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ...ml import TfIdfIndex
from ..base_apartment import BaseApartment


class KnowledgeAgent(BaseApartment):
    NAME = "knowledge"
    DESCRIPTION = "Agency brain: pricing, packages, FAQ, scripts, portfolio and guarantees (TF-IDF search)"
    PERMISSIONS = {"internet": False, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "knowledge.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        entries = self.load_shared_data("knowledge.json", default=None)
        if not entries or not isinstance(entries, dict):
            raise ValueError("data/knowledge.json missing or malformed")
        docs = entries.get("entries", [])
        if not docs:
            raise ValueError("data/knowledge.json has no entries")

        index = TfIdfIndex(min_df=1).fit(docs)
        self.save_model(self.MODEL, index.to_full_dict())
        self.remember("indexed", {"documents": len(docs), "vocab": len(index.vocab)})

        # Sanity check: the index must be able to answer its own questions.
        checks = self._self_check(index)
        self.remember("self_check", checks)
        self.log("trained", documents=len(docs), vocab=len(index.vocab),
                 passed=checks["passed"])
        return {"documents": len(docs), "vocab": len(index.vocab), "self_check": checks}

    def _self_check(self, index: TfIdfIndex) -> Dict[str, Any]:
        probes = [("kitna paisa lagega", "pricing"), ("free mockup", "trial"),
                  ("kitne din lagenge", "faq_time"), ("meri site pehle se hai", "script"),
                  ("portfolio dikhao", "portfolio")]
        passed = 0
        details = []
        for query, expected_prefix in probes:
            hits = index.query(query, top_k=1)
            got = (hits[0]["id"] if hits else "")
            ok = got.startswith(expected_prefix)
            passed += 1 if ok else 0
            details.append({"query": query, "expected": expected_prefix, "got": got, "ok": ok})
        return {"passed": passed, "total": len(probes), "details": details}

    # ----------------------------------------------------------------- search
    def index(self) -> Optional[TfIdfIndex]:
        data = self.load_model(self.MODEL, default={})
        if not data:
            return None
        try:
            return TfIdfIndex.from_dict(data)
        except Exception:
            return None

    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        index = self.index()
        if index is None:
            return []
        return index.query(query, top_k=top_k)

    def answer(self, query: str) -> str:
        """Best-matching knowledge entry, formatted for a human."""
        hits = self.search(query, top_k=1)
        if not hits:
            return "Is bare mein mere paas koi entry nahi hai. data/knowledge.json mein add kar dein."
        hit = hits[0]
        return "%s\n\n%s" % (hit.get("title", ""), hit.get("body", ""))

    def context(self, query: str, top_k: int = 2, max_chars: int = 700) -> str:
        """Short context block the orchestrator injects into other agents."""
        hits = self.search(query, top_k=top_k)
        if not hits:
            return ""
        blocks = []
        for hit in hits:
            body = re.sub(r"\s+", " ", hit.get("body", "")).strip()
            blocks.append("- %s: %s" % (hit.get("title", ""), body[:max_chars // top_k]))
        return "\n".join(blocks)

    # -------------------------------------------------------------- structured
    def agency(self) -> Dict[str, Any]:
        return super().agency() or {}

    def get_pricing(self, package: Optional[str] = None) -> Any:
        cfg = self.agency()
        packages = (cfg.get("packages") or {})
        if package:
            return packages.get(package, {})
        return packages

    def get_packages_summary(self) -> str:
        packages = self.get_pricing()
        lines = []
        for key in ("basic", "recommended", "premium"):
            data = packages.get(key, {})
            if not data:
                continue
            includes = ", ".join(data.get("includes", [])[:4])
            lines.append("%s: %s PKR (ya %s/mah) - %s" % (
                data.get("name", key), data.get("price", 0), data.get("monthly", 0), includes))
        return "\n".join(lines)

    def get_portfolio(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        cfg = self.agency()
        items = cfg.get("portfolio", []) or []
        if category:
            matches = [i for i in items if i.get("category") == category]
            return matches or items
        return items

    def get_portfolio_line(self, category: Optional[str] = None) -> str:
        items = self.get_portfolio(category)
        if not items:
            return "Karachi Biryani House (orders +40%)"
        item = items[0]
        return "%s, %s - %s" % (item.get("name", ""), item.get("city", ""),
                                item.get("result", ""))

    def get_faq(self) -> List[Dict[str, Any]]:
        entries = self.load_shared_data("knowledge.json", {}) or {}
        return [e for e in entries.get("entries", []) if "faq" in (e.get("tags") or [])]

    def get_guarantees(self) -> List[str]:
        cfg = self.agency()
        return cfg.get("guarantees", []) or []

    def outreach_rules(self) -> Dict[str, Any]:
        cfg = self.agency()
        return cfg.get("outreach_rules", {}) or {}

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()

        if any(word in lowered for word in ("package", "packages", "pricing", "rate",
                                            "qeem", "kitna", "price", "charges")):
            return "Packages:\n%s\n\nAdd-ons: logo 5,000 | content 8,000 | local SEO 12,000 | GMB 3,000 | domain+hosting 6,000 PKR" % self.get_packages_summary()
        if any(word in lowered for word in ("portfolio", "kaam", "previous work", "client")):
            items = self.get_portfolio()
            lines = ["%s (%s, %s): %s" % (i.get("name"), i.get("city"), i.get("category"),
                                          i.get("result")) for i in items]
            return "Portfolio:\n" + "\n".join(lines)
        if any(word in lowered for word in ("faq", "sawal", "question")):
            faqs = self.get_faq()
            return "\n\n".join("%s\n%s" % (f.get("title"), f.get("body")) for f in faqs[:4])
        if any(word in lowered for word in ("guarantee", "warranty", "refund", "trust")):
            return "Guarantees:\n" + "\n".join("- " + g for g in self.get_guarantees())
        if any(word in lowered for word in ("trial", "mockup", "free")):
            cfg = self.agency()
            trial = cfg.get("trial_policy", {}) or {}
            return "Trial policy: %s (%s ghante mein mockup, %s din refund)" % (
                trial.get("description", ""), trial.get("mockup_hours", 24),
                trial.get("refund_days", 7))

        hit = self.search(text, top_k=1)
        if hit:
            return "%s\n\n%s" % (hit[0].get("title", ""), hit[0].get("body", ""))
        return ("Knowledge base mein is sawal ka jawab nahi mila. "
                "data/knowledge.json mein naya entry add karein, phir train_all.py chalayein.")

    def status(self) -> Dict[str, Any]:
        data = super().status()
        data.update({"indexed": self.recall("indexed", {}),
                     "self_check": self.recall("self_check", {})})
        return data
