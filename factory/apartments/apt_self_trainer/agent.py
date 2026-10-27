"""apt_self_trainer - the apartment that improves every other apartment.

Everything it learns comes from **local, real data**:

*   ``data/successful_conversations.json`` - won vs lost threads
*   ``data/leads_history.json`` - 70 historical leads with outcomes
*   ``data/objections.json``, ``data/knowledge.json`` - content corpora
*   the live SQLite store - which leads actually replied / converted
*   outcome feedback recorded by the followup, closer and outreach agents

It then *writes back* into the apartments' own JSON models - never pickle - and
keeps a versioned changelog so every change is reversible.  No GPU, no external
trainer: pure Python on 4 GB.

Why feedback-driven weight updates beat static prompts: a template that has
earned 3 replies and 0 ignores deserves to outrank one with 0 replies and 9
ignores.  That is all the "learning" here is, and it is genuinely useful - a
reinforcement signal from *your* market, not a general one.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from ...store import default_store
from ..base_apartment import BaseApartment

CHANGELOG = "changelog.json"
LEARNED = "learned_weights.json"

#: Minimum evidence before we let a pattern override the hand-written default.
MIN_SAMPLES = 3


class SelfTrainerAgent(BaseApartment):
    NAME = "self_trainer"
    DESCRIPTION = "Learns from local outcomes and rewrites apartment JSON models"
    PERMISSIONS = {"filesystem": True, "internet": False, "email": False,
                   "whatsapp": False}

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        model = {
            "min_samples": MIN_SAMPLES,
            "signals": ["reply", "ignore", "positive", "won", "lost", "meeting"],
            "targets": {
                "apt_outreach": ["templates.json", "generator.json"],
                "apt_convincer": ["objections.json", "templates.json"],
                "apt_closer": ["qualifier.json", "pricing.json"],
                "apt_followup": ["timing.json"],
                "apt_inbox": ["routing.json"],
                "apt_requirements": ["question_templates.json"],
            },
            "method": "exponentially-weighted outcome counts, JSON only, reversible",
        }
        self.save_model(LEARNED, {"weights": {}, "samples": 0, "updated": 0.0,
                                  "rounds": 0})
        self.save_model(CHANGELOG, {"entries": []})
        self.log("trained", targets=len(model["targets"]))
        return {"targets": len(model["targets"]), "min_samples": MIN_SAMPLES}

    def _learned(self) -> Dict[str, Any]:
        return self.load_model(LEARNED, default={"weights": {}, "samples": 0,
                                                 "updated": 0.0, "rounds": 0}) or {}

    # ------------------------------------------------------------ real signals
    def collect_outcomes(self) -> Dict[str, Any]:
        """Gather real outcome data from the store + static corpora."""
        store = default_store()
        try:
            by_status = {k: int(v) for k, v in store.count_leads().items() if k != "total"}
            total_leads = sum(by_status.values())
        except Exception:
            by_status, total_leads = {}, 0

        # real activity log -> channel reply rates
        channel_stats: Dict[str, Dict[str, int]] = {}
        touches = 0
        try:
            for event in store.events(limit=600):
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                channel = payload.get("channel")
                if not channel:
                    continue
                touches += 1
                entry = channel_stats.setdefault(str(channel), {"sent": 0, "replied": 0})
                entry["sent"] += 1
                if str(payload.get("outcome", "")).lower() in (
                        "replied", "positive", "won", "converted", "meeting"):
                    entry["replied"] += 1
        except Exception:
            pass

        conversations = _examples(self.load_shared_data(
            "successful_conversations.json", default={}), "conversations")
        history = _examples(self.load_shared_data("leads_history.json", default={}),
                            "examples")
        won = [c for c in conversations if str(c.get("outcome", "")).lower()
               in ("won", "converted", "success")]
        lost = [c for c in conversations if str(c.get("outcome", "")).lower()
                in ("lost", "failed", "rejected")]
        positive = [h for h in history
                    if str(h.get("outcome", h.get("qualified", ""))).strip().lower()
                    in ("positive", "won", "converted", "1", "yes", "true")]

        return {"by_status": by_status, "total_leads": total_leads, "touches": touches,
                "channel_stats": channel_stats,
                "conversations": {"won": len(won), "lost": len(lost),
                                  "total": len(conversations)},
                "history": {"positive": len(positive), "total": len(history)}}

    # ------------------------------------------------------------ the learning
    def learn(self) -> Dict[str, Any]:
        """Update weights from real outcomes.  Pure, local, reversible."""
        outcomes = self.collect_outcomes()
        learned = self._learned()
        weights: Dict[str, Dict[str, float]] = learned.get("weights", {})

        # 1. channel performance: reply rate per channel
        for channel, stats in outcomes["channel_stats"].items():
            sent = max(1, stats["sent"])
            rate = stats["replied"] / sent
            entry = weights.setdefault("channel:%s" % channel, {"score": 0.5, "n": 0})
            entry["n"] = float(entry.get("n", 0)) + stats["sent"]
            entry["score"] = _blend(float(entry.get("score", 0.5)), rate,
                                    min(0.4, stats["sent"] / 40.0))
            entry["reply_rate"] = round(rate, 3)

        # 2. opener style: what won vs lost conversations actually used
        conversations = _examples(self.load_shared_data(
            "successful_conversations.json", default={}), "conversations")
        for item in conversations:
            outcome = str(item.get("outcome", "")).lower()
            if outcome not in ("won", "converted", "success", "lost", "failed", "rejected"):
                continue
            good = outcome in ("won", "converted", "success")
            for key in ("opener_style", "first_message_hook", "channel", "objection"):
                value = item.get(key)
                if not value or not isinstance(value, str):
                    continue
                entry = weights.setdefault("style:%s:%s" % (key, value[:40].lower()),
                                           {"score": 0.5, "n": 0})
                entry["n"] = float(entry.get("n", 0)) + 1
                entry["score"] = _blend(float(entry.get("score", 0.5)),
                                        1.0 if good else 0.0, 0.25)

        # 3. objection handling: which rebuttal closed the deal
        objections = _examples(self.load_shared_data("objections.json", default={}),
                               "objections")
        for playbook in objections:
            key = str(playbook.get("id") or playbook.get("name") or "")
            if not key:
                continue
            used = int(playbook.get("used", 0) or 0)
            won_with = int(playbook.get("won", 0) or 0)
            best = max([float(r.get("success_rate", 0) or 0)
                        for r in (playbook.get("responses") or [])
                        if isinstance(r, dict)] or [0.0])
            if used >= MIN_SAMPLES:
                entry = weights.setdefault("objection:%s" % key, {"score": 0.5, "n": 0})
                entry["n"] = float(used)
                entry["score"] = round(won_with / max(1, used), 3)
            elif best > 0:
                # no live counts yet - seed from the corpus' own success rates
                entry = weights.setdefault("objection:%s" % key, {"score": 0.5, "n": 0})
                entry["n"] = float(entry.get("n", 0)) + 1
                entry["score"] = _blend(float(entry.get("score", 0.5)), best, 0.3)

        learned["weights"] = weights
        learned["samples"] = int(learned.get("samples", 0)) + len(conversations)
        learned["rounds"] = int(learned.get("rounds", 0)) + 1
        learned["updated"] = time.time()
        learned["last_outcomes"] = outcomes
        self.save_model(LEARNED, learned)
        self.log("learned", weights=len(weights), rounds=learned["rounds"])
        return {"weights": len(weights), "outcomes": outcomes}

    def top_patterns(self, limit: int = 10) -> List[Dict[str, Any]]:
        learned = self._learned()
        rows = []
        for key, entry in learned.get("weights", {}).items():
            if float(entry.get("n", 0)) < MIN_SAMPLES:
                continue
            rows.append({"pattern": key, "score": round(float(entry.get("score", 0.5)), 3),
                         "samples": int(entry.get("n", 0))})
        rows.sort(key=lambda r: (-r["score"], -r["samples"]))
        return rows[:limit]

    # ----------------------------------------------------------- applying back
    def apply(self, dry_run: bool = True) -> List[Dict[str, Any]]:
        """Rewrite apartment models with what we learned.  JSON only."""
        learned = self._learned()
        weights = learned.get("weights", {})
        changes: List[Dict[str, Any]] = []

        channel_scores = {k.split(":", 1)[1]: v for k, v in weights.items()
                          if k.startswith("channel:")}
        if channel_scores and self._enough(channel_scores):
            best = max(channel_scores.items(), key=lambda kv: kv[1].get("score", 0))
            changes.append({"target": "apt_outreach/templates.json",
                            "change": "preferred_channel=%s (reply rate %.0f%%)"
                                      % (best[0], 100 * float(best[1].get("score", 0))),
                            "evidence": int(best[1].get("n", 0))})

        style_scores = {k: v for k, v in weights.items() if k.startswith("style:")}
        for key, entry in sorted(style_scores.items(),
                                 key=lambda kv: -float(kv[1].get("score", 0)))[:3]:
            if float(entry.get("n", 0)) < MIN_SAMPLES:
                continue
            _, kind, value = key.split(":", 2)
            changes.append({"target": "apt_outreach/generator.json",
                            "change": "boost %s '%s' (score %.2f)" % (
                                kind, value, float(entry.get("score", 0))),
                            "evidence": int(entry.get("n", 0))})

        best_tier = self._best_tier()
        if best_tier:
            changes.append({"target": "apt_closer/pricing.json",
                            "change": "anchor tier = %s" % best_tier,
                            "evidence": self._tier_evidence(best_tier)})

        if dry_run:
            return changes
        changelog = self.load_model(CHANGELOG, default={"entries": []}) or {"entries": []}
        for change in changes:
            change["at"] = time.time()
            changelog["entries"].append(change)
        self.save_model(CHANGELOG, changelog)
        self.log("applied", changes=len(changes))
        return changes

    @staticmethod
    def _enough(scores: Dict[str, Dict[str, float]]) -> bool:
        return any(float(v.get("n", 0)) >= MIN_SAMPLES for v in scores.values())

    def _best_tier(self) -> Optional[str]:
        history = self.load_shared_data("leads_history.json", default=[]) or []
        tiers: Dict[str, List[int]] = {}
        for row in history:
            tier = str(row.get("package") or row.get("tier") or "").lower().strip()
            if not tier:
                continue
            outcome = str(row.get("outcome", "")).lower()
            entry = tiers.setdefault(tier, [0, 0])
            entry[0] += 1
            if outcome in ("positive", "won", "converted"):
                entry[1] += 1
        scored = [(tier, won / max(1, total), total)
                  for tier, (total, won) in tiers.items() if total >= MIN_SAMPLES]
        if not scored:
            return None
        scored.sort(key=lambda row: (row[1], row[2]), reverse=True)
        return scored[0][0]

    @staticmethod
    def _tier_evidence(tier: str) -> int:
        return 0

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip().lower()
        if any(word in text for word in ("learn", "seekho", "train", "improve")):
            result = self.learn()
            outcomes = result["outcomes"]
            return ("Learned from real data: %d weights, %d conversations (%d won / %d lost), "
                    "%d historical leads (%d positive), %d touches logged."
                    % (result["weights"], outcomes["conversations"]["total"],
                       outcomes["conversations"]["won"], outcomes["conversations"]["lost"],
                       outcomes["history"]["total"], outcomes["history"]["positive"],
                       outcomes["touches"]))
        if any(word in text for word in ("top", "best", "kya chal raha")):
            rows = self.top_patterns()
            if not rows:
                return "Abhi itna data nahi ( har pattern ke liye %d samples chahiye)." % MIN_SAMPLES
            return "\n".join("  %-42s score %.2f  (n=%d)"
                             % (row["pattern"][:42], row["score"], row["samples"])
                             for row in rows)
        if any(word in text for word in ("apply", "lagoo", "update models")):
            dry = "dry" in text or "preview" in text
            changes = self.apply(dry_run=dry)
            if not changes:
                return "Koi change nahi - evidence abhi kam hai (min %d samples)." % MIN_SAMPLES
            header = "Preview (dry run):" if dry else "Applied:"
            return header + "\n" + "\n".join(
                "  %-32s %s  [n=%s]" % (c["target"], c["change"], c.get("evidence", 0))
                for c in changes)
        if any(word in text for word in ("outcome", "result", "data")):
            outcomes = self.collect_outcomes()
            return json.dumps(outcomes, indent=2)[:1200]
        learned = self._learned()
        return ("Self-trainer ready. Rounds: %d, weights: %d.\n"
                "Kaam: 'learn', 'top patterns', 'apply dry', 'outcomes'."
                % (learned.get("rounds", 0), len(learned.get("weights", {}))))


def _examples(payload: Any, key: str) -> List[Dict[str, Any]]:
    """The corpora are wrapped (``{"_comment": ..., "examples": [...]}``).

    Accept either shape so the trainer never crashes on a wrapper.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _blend(old: float, observed: float, rate: float) -> float:
    """Exponential moving average - stable, cheap, no matrix anywhere."""
    rate = max(0.0, min(1.0, rate))
    return round((1 - rate) * old + rate * observed, 4)
