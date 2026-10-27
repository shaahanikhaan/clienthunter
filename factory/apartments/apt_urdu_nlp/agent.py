"""apt_urdu_nlp - the language brain.

Trains from ``data/urdu_training.json`` (200+ examples shipped, add your own)
and produces, for any message:

    intent      price_query | requirement_query | objection | interest |
                not_interested | callback_request | human_needed
    sentiment   positive | neutral | negative  (model + lexicon, negation-aware)
    priority    high | medium | low
    entities    prices, phones, emails, URLs, dates, names, cities, categories
    language    urdu | roman_urdu | english | mixed

Runs on CPU in ~4 MB.  An optional ``jev-urdu`` transformer upgrade is detected
but disabled by default: it needs torch + a downloaded model, which does not fit
the 4 GB / no-external-download rules.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base_apartment import BaseApartment
from ..tools.nlp_urdu import (INTENTS, PRIORITIES, SENTIMENTS, UrduNlpModel,
                              detect_language, extract_entities, tokenize,
                              try_load_jev_urdu)


class UrduNlpAgent(BaseApartment):
    NAME = "urdu_nlp"
    DESCRIPTION = "Urdu + Roman Urdu understanding: intent, sentiment, priority, entities"
    PERMISSIONS = {"internet": False, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "urdu_nlp.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        data = self.load_shared_data("urdu_training.json", default=None)
        if not data or not isinstance(data, dict):
            raise ValueError("data/urdu_training.json missing or malformed")
        examples = data.get("examples", [])
        if len(examples) < 20:
            raise ValueError("need at least 20 training examples, got %d" % len(examples))

        model = UrduNlpModel.train(examples)
        self.save_model(self.MODEL, model.to_dict())

        # Held-out accuracy via a deterministic 80/20 split.
        accuracy = self._evaluate(examples)
        stats = {
            "examples": len(examples),
            "intents": sorted({e.get("intent", "") for e in examples}),
            "sentiments": sorted({e.get("sentiment", "") for e in examples}),
            "vocab": model.meta.get("vocab", 0),
            "intent_accuracy": accuracy["intent"],
            "sentiment_accuracy": accuracy["sentiment"],
            "samples_tested": accuracy["tested"],
        }
        self.remember("training_stats", stats)
        self.log("trained", **{k: v for k, v in stats.items() if not isinstance(v, list)})
        return stats

    def _evaluate(self, examples: List[Dict[str, Any]]) -> Dict[str, Any]:
        from ...ml import train_test_split
        from ..tools.nlp_urdu import UrduNlpModel, features

        train, test = train_test_split(examples, ratio=0.8, seed=13)
        if len(test) < 5:
            return {"intent": 0.0, "sentiment": 0.0, "tested": 0}
        model = UrduNlpModel.train(train)
        intent_hits = sentiment_hits = 0
        for example in test:
            feats = features(example.get("text", ""))
            intent, _ = model.intent.predict(feats)
            sentiment, _ = model.sentiment.predict(feats)
            intent_hits += 1 if intent == example.get("intent") else 0
            sentiment_hits += 1 if sentiment == example.get("sentiment") else 0
        total = len(test)
        return {"intent": round(intent_hits / total, 3),
                "sentiment": round(sentiment_hits / total, 3), "tested": total}

    # ----------------------------------------------------------------- analyse
    def nlp(self):
        return self.tool("nlp_urdu")

    def analyse(self, text: str) -> Dict[str, Any]:
        result = self.nlp().analyse(text)
        self.log("analyse", text=text[:60], intent=result.get("intent"),
                 sentiment=result.get("sentiment"), priority=result.get("priority"))
        return result

    def classify_intent(self, text: str) -> str:
        return self.analyse(text)["intent"]

    def detect_sentiment(self, text: str) -> str:
        return self.analyse(text)["sentiment"]

    def extract_entities(self, text: str) -> Dict[str, Any]:
        return extract_entities(text)

    def tokenize_urdu(self, text: str) -> List[str]:
        return tokenize(text)

    def batch(self, texts: List[str]) -> List[Dict[str, Any]]:
        return [self.analyse(text) for text in texts]

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        if not text:
            return "Koi text do, main us ka intent, sentiment aur entities nikal kar bataonga."
        result = self.analyse(text)
        entities = result["entities"]
        lines = [
            "Language : %s" % result["language"],
            "Intent   : %s (%.0f%% confidence)" % (result["intent"],
                                                   result["intent_confidence"] * 100),
            "Sentiment: %s (score %s)" % (result["sentiment"], result["sentiment_score"]),
            "Priority : %s" % result["priority"],
        ]
        found = []
        for key, label in (("prices", "Price"), ("phones", "Phone"), ("emails", "Email"),
                           ("urls", "URL"), ("dates", "Date"), ("names", "Name"),
                           ("cities", "City"), ("categories", "Category")):
            values = entities.get(key) or []
            if not values:
                continue
            if key == "prices":
                rendered = ", ".join("%s %s" % (v["value"], v["currency"]) for v in values)
            elif key == "dates":
                rendered = ", ".join(str(v.get("raw", "")) for v in values)
            else:
                rendered = ", ".join(str(v) for v in values)
            found.append("%-9s: %s" % (label, rendered))
        if found:
            lines.append("")
            lines.extend(found)
        if result.get("untrained"):
            lines.append("")
            lines.append("(Note: model trained nahi hai - sirf rules use ho rahe hain. "
                         "train_all.py chalayein.)")
        return "\n".join(lines)

    def status(self) -> Dict[str, Any]:
        data = super().status()
        data["training_stats"] = self.recall("training_stats", {})
        data["jev_urdu_available"] = try_load_jev_urdu() is not None
        return data
