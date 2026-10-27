"""Train every model in the factory from the local JSON corpora.

Ten trainers, all pure Python, all CPU, all under the 4 GB budget:

 1. IntentRouterTrainer        multinomial Naive Bayes (text -> apartment)
 2. UrduNLPTrainer             Urdu + Roman Urdu: intents, sentiment, entities
 3. TFIDFKnowledgeTrainer      TF-IDF index over data/knowledge.json
 4. TrustScriptTrainer         objection playbooks (objections.json)
 5. LeadQualifierTrainer       logistic regression **written from scratch**
 6. ObjectionHandlerTrainer    rebuttal banks + proof points
 7. ResponseGeneratorTrainer   n-gram / Markov generator (successful_conversations.json)
 8. LocalLLMFineTuner          n-gram language model + optional GGUF adapter
 9. PlatformAgentTrainer       per-platform playbooks from platform_posts.json
10. SelfTrainer                outcome weights from leads_history + store

Every model is saved as **JSON** (never pickle) into
``apartments/apt_<name>/models/`` so you can read it, diff it and version it.

    python -m factory.train_all              # train everything
    python -m factory.train_all --force      # retrain even if models exist
    python -m factory.train_all --only hunter,closer
    python -m factory.train_all --verify     # load every model back and check it
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ml import LogisticRegression, TfIdfIndex, tokenize, train_test_split
from .registry import APARTMENTS, TRAIN_ORDER
from .store import default_store

DATA = Path(__file__).resolve().parent / "data"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load(name: str, default: Any = None) -> Any:
    path = DATA / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write("  ! cannot parse data/%s: %s\n" % (name, exc))
        return default


def _rows(name: str, key: str = "examples") -> List[Dict[str, Any]]:
    """Return the example list from one of the corpora, whatever its wrapper."""
    payload = _load(name, None)
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for candidate in (key, "entries", "conversations", "objections", "cities"):
            value = payload.get(candidate)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _split_pairs(x_rows: List[Any], y_rows: List[Any], test_size: float = 0.2):
    """Paired train/test split - ``ml.train_test_split`` takes one sequence."""
    pairs = list(zip(x_rows, y_rows))
    train, test = train_test_split(pairs, ratio=1.0 - test_size)
    return ([x for x, _ in train], [x for x, _ in test],
            [y for _, y in train], [y for _, y in test])


def _apartment(name: str):
    """Import one apartment class and instantiate it (no factory boot)."""
    module_suffix, class_name = APARTMENTS[name]
    import importlib

    module = importlib.import_module("factory.apartments." + module_suffix)
    return getattr(module, class_name)()


def _roman_markers(text: str) -> List[str]:
    """Extra features that matter for Roman Urdu: negation, politeness, money."""
    lowered = (text or "").lower()
    markers = []
    if re.search(r"\b(nahi|nahin|nhi|mat|na)\b", lowered):
        markers.append("__neg__")
    if re.search(r"\b(mehnga|mehenga|expensive|costly|budget|paisay|paisa|rate|price)\b",
                 lowered):
        markers.append("__money__")
    if re.search(r"\b(sochna|soch|time|baad|kal|later|wait)\b", lowered):
        markers.append("__delay__")
    if re.search(r"\b(jaldi|urgent|abhi|today|aaj|fast)\b", lowered):
        markers.append("__urgent__")
    if re.search(r"[\u0600-\u06FF]", text or ""):
        markers.append("__urdu_script__")
    if re.search(r"\?|kya|kaun|kab|kitna|kesa", lowered):
        markers.append("__question__")
    return markers


def _timer():
    started = time.time()
    return lambda: round(time.time() - started, 2)


# --------------------------------------------------------------------------- #
# 1. Intent router
# --------------------------------------------------------------------------- #
class IntentRouterTrainer:
    """Multinomial Naive Bayes over ``data/router_train.json``.

    Written out longhand (no sklearn): class priors + per-token log
    likelihoods with additive smoothing, exactly the textbook estimator.
    """

    MODEL = "router.json"
    SMOOTHING = 0.2

    def train(self, apartment) -> Dict[str, Any]:
        rows = _rows("router_train.json")
        if not rows:
            raise RuntimeError("data/router_train.json has no examples")

        texts, labels = [], []
        for row in rows:
            text = str(row.get("text") or row.get("message") or "")
            label = str(row.get("apartment") or row.get("label") or "")
            if text and label:
                texts.append(text)
                labels.append(label)

        known = set(APARTMENTS)
        labels = [label if label in known else "knowledge" for label in labels]

        train_x, test_x, train_y, test_y = _split_pairs(texts, labels)
        counts: Dict[str, Counter] = defaultdict(Counter)
        class_counts: Counter = Counter()
        for text, label in zip(train_x, train_y):
            class_counts[label] += 1
            features = tokenize(text) + _roman_markers(text)
            counts[label].update(features)

        vocabulary = sorted({token for counter in counts.values() for token in counter})
        total_docs = sum(class_counts.values())
        priors = {label: count / total_docs for label, count in class_counts.items()}
        vocab_size = max(1, len(vocabulary))

        log_likelihoods: Dict[str, Dict[str, float]] = {}
        for label, counter in counts.items():
            denominator = sum(counter.values()) + self.SMOOTHING * vocab_size
            log_likelihoods[label] = {
                token: math.log((count + self.SMOOTHING) / denominator)
                for token, count in counter.items()}

        # held-out accuracy, computed with the same estimator
        correct = 0
        for text, label in zip(test_x, test_y):
            guess = self._predict(priors, log_likelihoods, vocab_size, text)
            correct += int(guess == label)
        accuracy = correct / max(1, len(test_x))

        apartment.save_model(self.MODEL, {
            "classes": sorted(priors),
            "priors": priors,
            "log_likelihoods": log_likelihoods,
            "vocabulary": vocabulary,
            "smoothing": self.SMOOTHING,
            "examples": len(texts),
            "train_size": len(train_x),
            "test_size": len(test_x),
            "holdout_accuracy": round(accuracy, 3),
            "trained_from": "data/router_train.json",
        })
        return {"examples": len(texts), "labels": len(priors),
                "holdout_accuracy": round(accuracy, 3)}

    @staticmethod
    def _predict(priors, log_likelihoods, vocab_size, text: str) -> str:
        tokens = tokenize(text) + _roman_markers(text)
        best, best_score = "knowledge", -1e18
        default = math.log(0.2 / vocab_size)
        for label, prior in priors.items():
            score = math.log(max(1e-9, prior))
            table = log_likelihoods.get(label, {})
            for token in tokens:
                score += table.get(token, default)
            if score > best_score:
                best, best_score = label, score
        return best


# --------------------------------------------------------------------------- #
# 2. Urdu NLP
# --------------------------------------------------------------------------- #
class UrduNLPTrainer:
    """Intents, sentiment and entity cues from ``data/urdu_training.json``."""

    def train(self, apartment) -> Dict[str, Any]:
        rows = _rows("urdu_training.json")
        intents: Dict[str, Counter] = defaultdict(Counter)
        sentiments: Dict[str, Counter] = defaultdict(Counter)
        entities: Dict[str, Counter] = defaultdict(Counter)
        intent_totals: Counter = Counter()
        sentiment_totals: Counter = Counter()
        vocabulary: Counter = Counter()
        examples = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or row.get("message") or "")
            intent = str(row.get("intent") or "").strip()
            sentiment = str(row.get("sentiment") or "").strip()
            if not text:
                continue
            examples += 1
            tokens = tokenize(text) + _roman_markers(text)
            vocabulary.update(tokens)
            if intent:
                intent_totals[intent] += 1
                intents[intent].update(tokens)
            if sentiment:
                sentiment_totals[sentiment] += 1
                sentiments[sentiment].update(tokens)
            for entity in (row.get("entities") or {}):
                if isinstance(row["entities"], dict):
                    entities[str(entity)].update(
                        [str(row["entities"][entity])])
                break

        def log_probs(table: Dict[str, Counter], totals: Counter,
                      smoothing: float = 0.3):
            vocabulary_size = max(1, len(vocabulary))
            out: Dict[str, Dict[str, float]] = {}
            for label, counter in table.items():
                denominator = sum(counter.values()) + smoothing * vocabulary_size
                out[label] = {token: round(math.log((count + smoothing) / denominator), 5)
                              for token, count in counter.most_common(400)}
            return {"priors": {label: round(count / max(1, sum(totals.values())), 5)
                               for label, count in totals.items()},
                    "log_likelihoods": out}

        model = {
            "vocabulary": [token for token, _ in vocabulary.most_common(1500)],
            "intents": log_probs(intents, intent_totals),
            "sentiments": log_probs(sentiments, sentiment_totals),
            "entities": {key: counter.most_common(30)
                         for key, counter in entities.items()},
            "examples": examples,
            "smoothing": 0.3,
            "trained_from": "data/urdu_training.json",
            "script": {"urdu_range": "0600-06FF", "detects_roman_urdu": True},
        }
        apartment.save_model("urdu_nlp.json", model)
        return {"examples": examples, "intents": len(intent_totals),
                "sentiments": len(sentiment_totals), "vocab": len(vocabulary)}


# --------------------------------------------------------------------------- #
# 3. Knowledge TF-IDF
# --------------------------------------------------------------------------- #
class TFIDFKnowledgeTrainer:
    def train(self, apartment) -> Dict[str, Any]:
        entries = _rows("knowledge.json", "entries")
        if not entries:
            raise RuntimeError("data/knowledge.json has no entries")
        docs = []
        for entry in entries:
            docs.append({
                "id": entry.get("id") or str(len(docs)),
                "title": entry.get("title") or entry.get("question") or "",
                "body": " ".join(str(entry.get(key, ""))
                                 for key in ("body", "answer", "text", "content")),
                "tags": entry.get("tags") or [],
                "entry": entry,
            })
        index = TfIdfIndex()
        index.fit(docs)
        payload = index.to_full_dict()
        payload.setdefault("documents", len(docs))
        payload["documents"] = len(docs)
        payload["trained_from"] = "data/knowledge.json"
        apartment.save_model("knowledge_index.json", payload)
        return {"documents": len(docs), "vocab": len(payload.get("idf", {}))}


# --------------------------------------------------------------------------- #
# 4. Trust scripts / objection playbooks
# --------------------------------------------------------------------------- #
class TrustScriptTrainer:
    def train(self, apartment) -> Dict[str, Any]:
        items = _rows("objections.json", "objections")
        model = {"objections": {}, "proof_points": [], "trust_lines": [],
                 "count": len(items), "trained_from": "data/objections.json"}
        for item in items:
            key = str(item.get("id") or item.get("name") or item.get("key") or "")[:60]
            if not key:
                continue
            ranked = sorted(
                (response for response in (item.get("responses") or [])
                 if isinstance(response, dict)),
                key=lambda r: -float(r.get("success_rate", 0) or 0))
            model["objections"][key] = {
                "name": item.get("name", key),
                "priority": item.get("priority", "medium"),
                "phrases": item.get("signals") or item.get("phrases") or [],
                "rebuttals": [r.get("text", "") for r in ranked],
                "styles": [r.get("style", "") for r in ranked],
                "success_rates": [float(r.get("success_rate", 0) or 0) for r in ranked],
                "proof": item.get("proof") or [],
                "used": int(item.get("used", 0) or 0),
                "won": int(item.get("won", 0) or 0),
            }
        apartment.save_model("objections.json", model)
        return {"objections": len(model["objections"])}


# --------------------------------------------------------------------------- #
# 5. Lead qualifier - logistic regression from scratch
# --------------------------------------------------------------------------- #
class LeadQualifierTrainer:
    """Trains the closer's close-probability model on ``data/leads_history.json``.

    Features are hand-built (not text): has_email, has_phone, has_website,
    responded, audit_score, rating, reviews, city tier, category budget.
    """

    def train(self, apartment) -> Dict[str, Any]:
        payload = _load("leads_history.json", {}) or {}
        rows = [row for row in (payload.get("examples")
                                if isinstance(payload, dict) else payload or [])
                if isinstance(row, dict)]
        if not rows:
            raise RuntimeError("data/leads_history.json has no examples")

        feature_names = list(payload.get("features") or self.FALLBACK_FEATURES)
        label_name = str(payload.get("label") or "qualified")

        x_rows: List[List[float]] = []
        y_rows: List[int] = []
        for row in rows:
            vector = []
            for name in feature_names:
                value = row.get(name, 0)
                if isinstance(value, bool):
                    value = 1.0 if value else 0.0
                elif isinstance(value, str):
                    value = 1.0 if value.strip().lower() in ("1", "yes", "true") else 0.0
                try:
                    vector.append(float(value or 0))
                except (TypeError, ValueError):
                    vector.append(0.0)
            x_rows.append(vector)
            raw_label = row.get(label_name, 0)
            y_rows.append(1 if str(raw_label).strip().lower() in ("1", "yes", "true")
                          else int(bool(raw_label)))

        # normalise the two columns that are on a raw scale
        self._standardise(x_rows, feature_names, ("rating", "reviews", "response_hours"))

        train_x, test_x, train_y, test_y = _split_pairs(x_rows, y_rows)
        model = LogisticRegression(learning_rate=0.2, epochs=500, l2=1e-3)
        model.fit(train_x, train_y, feature_names=feature_names)
        correct = sum(1 for row, label in zip(test_x, test_y)
                      if (1 if model.predict_proba(row) >= 0.5 else 0) == label)
        accuracy = correct / max(1, len(test_y))

        saved = dict(model.to_dict())
        saved.update({"features": feature_names, "label": label_name,
                      "examples": len(rows), "train_size": len(train_x),
                      "test_size": len(test_x),
                      "holdout_accuracy": round(accuracy, 3),
                      "positive_rate": round(sum(y_rows) / max(1, len(y_rows)), 3),
                      "trained_from": "data/leads_history.json"})
        apartment.save_model("qualifier.json", saved)
        return {"examples": len(rows), "features": len(feature_names),
                "holdout_accuracy": round(accuracy, 3)}

    FALLBACK_FEATURES = ("has_phone", "has_email", "has_website", "rating", "reviews",
                         "responded", "response_hours", "business_size", "budget_signal",
                         "timeline_signal", "decision_maker", "category_weight")

    @staticmethod
    def _standardise(rows: List[List[float]], names: List[str], targets) -> None:
        """Put raw-scale columns on one footing (z-score) so LR behaves."""
        for name in targets:
            if name not in names:
                continue
            index = names.index(name)
            values = [row[index] for row in rows]
            mean = sum(values) / max(1, len(values))
            variance = sum((v - mean) ** 2 for v in values) / max(1, len(values))
            std = math.sqrt(variance) or 1.0
            for row in rows:
                row[index] = (row[index] - mean) / std


# --------------------------------------------------------------------------- #
# 6. Objection handler
# --------------------------------------------------------------------------- #
class ObjectionHandlerTrainer:
    """Per-objection rebuttal ranking for the convincer."""

    def train(self, apartment) -> Dict[str, Any]:
        items = _rows("objections.json", "objections")
        bank: Dict[str, Any] = {}
        for item in items:
            key = str(item.get("id") or item.get("name") or "")[:60]
            if not key:
                continue
            ranked = sorted(
                (response for response in (item.get("responses") or [])
                 if isinstance(response, dict)),
                key=lambda r: -float(r.get("success_rate", 0) or 0))
            bank[key] = {
                "name": item.get("name", key),
                "triggers": [str(signal).lower()
                             for signal in (item.get("signals") or [])],
                "rebuttals": [r.get("text", "") for r in ranked],
                "styles": [r.get("style", "") for r in ranked],
                "success_rate": max([float(r.get("success_rate", 0) or 0)
                                     for r in ranked] or [0.0]),
                "proof": item.get("proof") or [],
            }
        apartment.save_model("objection_bank.json", {
            "bank": bank, "count": len(bank),
            "trained_from": "data/objections.json"})
        return {"objections": len(bank)}


# --------------------------------------------------------------------------- #
# 7. Response generator (n-gram / Markov)
# --------------------------------------------------------------------------- #
class ResponseGeneratorTrainer:
    """Learns opener/closer phrasing from real won conversations."""

    def train(self, apartment) -> Dict[str, Any]:
        rows = _rows("successful_conversations.json", "conversations")
        openers: Counter = Counter()
        closers: Counter = Counter()
        bigrams: Dict[str, Counter] = defaultdict(Counter)
        won_openers: Counter = Counter()
        for row in rows:
            outcome = str(row.get("outcome", "")).lower()
            good = outcome in ("won", "converted", "success")
            messages = row.get("messages") or row.get("thread") or []
            if isinstance(messages, str):
                messages = [{"text": messages}]
            texts = [str(m.get("text", "")) for m in messages if isinstance(m, dict)]
            texts = [t for t in texts if t.strip()]
            if not texts:
                continue
            first, last = texts[0], texts[-1]
            openers[first[:200]] += 1
            closers[last[:200]] += 1
            if good:
                won_openers[first[:200]] += 1
            tokens = tokenize(" ".join(texts))
            for left, right in zip(tokens, tokens[1:]):
                bigrams[left][right] += 1
        model = {
            "openers": openers.most_common(60),
            "closers": closers.most_common(60),
            "best_openers": won_openers.most_common(20),
            "bigrams": {left: counter.most_common(12)
                        for left, counter in list(bigrams.items())[:800]},
            "conversations": len(rows),
            "trained_from": "data/successful_conversations.json",
        }
        apartment.save_model("generator.json", model)
        return {"conversations": len(rows), "bigrams": len(model["bigrams"])}


# --------------------------------------------------------------------------- #
# 8. Local LLM / n-gram language model
# --------------------------------------------------------------------------- #
class LocalLLMFineTuner:
    """Character + word trigram model for Roman Urdu drafting.

    CPU-only and tiny by design.  If a GGUF model is ever dropped into
    ``factory/models/`` the runtime tool (``tools/llm_local.py``) uses it as a
    tier-2 backend; this trainer only ever writes JSON weights.
    """

    def train(self, apartment) -> Dict[str, Any]:
        corpus: List[str] = []
        for name, key in (("successful_conversations.json", "conversations"),
                          ("objections.json", "objections"),
                          ("knowledge.json", "entries"),
                          ("urdu_training.json", "examples")):
            rows = _rows(name, key)
            for row in rows:
                if isinstance(row, dict):
                    corpus.append(" ".join(
                        str(row.get(key, "")) for key in
                        ("text", "answer", "question", "rebuttal", "opener", "body",
                         "title", "name")
                        if row.get(key)))
                    for message in (row.get("messages") or []):
                        if isinstance(message, dict):
                            corpus.append(str(message.get("text", "")))
                    for response in (row.get("responses") or []):
                        if isinstance(response, dict):
                            corpus.append(str(response.get("text", "")))
                elif isinstance(row, str):
                    corpus.append(row)
        text = " ".join(part for part in corpus if part)
        tokens = tokenize(text)[:120_000]          # RAM guard
        trigrams: Dict[str, Counter] = defaultdict(Counter)
        for a, b, c in zip(tokens, tokens[1:], tokens[2:]):
            trigrams["%s %s" % (a, b)][c] += 1
        model = {
            "order": 3,
            "tokens_seen": len(tokens),
            "vocab": len(set(tokens)),
            "trigrams": {key: counter.most_common(8)
                         for key, counter in list(trigrams.items())[:6000]},
            "backend": "ngram-json (tier-1) / optional GGUF (tier-2, needs llama-cpp)",
            "note": "No GPU, no downloads. 4 GB safe.",
        }
        apartment.save_model("ngram.json", model)
        return {"trigrams": len(model["trigrams"]), "tokens": len(tokens)}


# --------------------------------------------------------------------------- #
# 9. Platform agents
# --------------------------------------------------------------------------- #
class PlatformAgentTrainer:
    """Splits ``data/platform_posts.json`` into per-platform playbooks."""

    def train(self, apartments: Dict[str, Any]) -> Dict[str, Any]:
        data = _load("platform_posts.json", {}) or {}
        platforms = data.get("platforms") if isinstance(data, dict) else data
        platforms = platforms or {}
        saved = {}
        mapping = {
            "whatsapp": "platform_whatsapp",
            "linkedin": "platform_linkedin",
            "facebook": "platform_facebook",
            "instagram": "platform_instagram",
            "email": "outreach",
        }
        for key, playbook in platforms.items():
            target = mapping.get(str(key).lower())
            apartment = apartments.get(target) if target else None
            if apartment is None or not isinstance(playbook, dict):
                continue
            model = {
                "platform": key,
                "daily_limit": int(playbook.get("daily_limit", 15)),
                "max_words": int(playbook.get("max_words", 80)),
                "best_hours": playbook.get("best_hours", []),
                "best_days": playbook.get("best_days", []),
                "avoid_hours": playbook.get("avoid_hours", []),
                "style": playbook.get("style", ""),
                "opener_pattern": playbook.get("opener_pattern", ""),
                "templates": playbook.get("templates", []),
                "notes": playbook.get("notes", ""),
                "trained_from": "data/platform_posts.json",
            }
            apartment.save_model("playbook.json", model)
            saved[key] = len(model["templates"])
        return saved


# --------------------------------------------------------------------------- #
# 10. Self trainer (outcome weights)
# --------------------------------------------------------------------------- #
class SelfTrainerRunner:
    def train(self, apartment) -> Dict[str, Any]:
        result = apartment.learn()
        outcomes = result.get("outcomes", {}) or {}
        return {"weights": result.get("weights", 0),
                "leads": outcomes.get("total_leads", 0),
                "touches": outcomes.get("touches", 0),
                "won": (outcomes.get("conversations") or {}).get("won", 0),
                "lost": (outcomes.get("conversations") or {}).get("lost", 0)}


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
#: apartment -> list of trainers (in order)
PLAN: Dict[str, List[Tuple[str, Any]]] = {
    "knowledge": [("tfidf-knowledge", TFIDFKnowledgeTrainer()),
                  ("intent-router", IntentRouterTrainer())],
    "urdu_nlp": [("urdu-nlp", UrduNLPTrainer())],
    "convincer": [("trust-scripts", TrustScriptTrainer()),
                  ("objection-handler", ObjectionHandlerTrainer())],
    "closer": [("lead-qualifier", LeadQualifierTrainer())],
    "outreach": [("response-generator", ResponseGeneratorTrainer()),
                 ("local-llm", LocalLLMFineTuner())],
    "self_trainer": [("self-trainer", SelfTrainerRunner())],
}


def run(only: Optional[Iterable[str]] = None, force: bool = False,
        verify: bool = False, quiet: bool = False) -> Dict[str, Any]:
    wanted = [name for name in (only or list(TRAIN_ORDER)) if name in APARTMENTS]
    apartments: Dict[str, Any] = {}
    results: Dict[str, Any] = {}
    started = time.time()

    # every apartment trains itself first (its own JSON models)
    for name in wanted:
        apartment = _apartment(name)
        apartments[name] = apartment
        elapsed = _timer()
        try:
            if not force and apartment.is_trained() and name not in PLAN and \
                    name not in ("platform_whatsapp", "platform_linkedin",
                                 "platform_facebook", "platform_instagram"):
                results[name] = {"skipped": True}
                if not quiet:
                    print("  %-22s %-18s skipped (already trained)"
                          % ("apt_" + name, "self"))
                continue
            stats = apartment.train()
            results.setdefault(name, {})["self"] = stats
            if not quiet:
                print("  %-22s %-18s ok  %s"
                      % ("apt_" + name, "self", _brief(stats)))
        except Exception as exc:
            results.setdefault(name, {})["self"] = {"error": str(exc)[:200]}
            if not quiet:
                print("  %-22s %-18s FAILED %s" % ("apt_" + name, "self", exc))
        results[name]["_seconds"] = elapsed()

    # shared trainers that write into several apartments at once
    for owner, trainers in PLAN.items():
        if only and owner not in wanted:
            continue
        apartment = apartments.get(owner) or _apartment(owner)
        apartments.setdefault(owner, apartment)
        for label, trainer in trainers:
            elapsed = _timer()
            try:
                if label == "platform-agents":
                    stats = trainer.train(apartments)
                else:
                    stats = trainer.train(apartment)
                results.setdefault(owner, {})[label] = stats
                if not quiet:
                    print("  %-22s %-18s ok  %s"
                          % ("apt_" + owner, label, _brief(stats)))
            except Exception as exc:
                results.setdefault(owner, {})[label] = {"error": str(exc)[:200]}
                if not quiet:
                    print("  %-22s %-18s FAILED %s" % ("apt_" + owner, label, exc))
            results[owner]["_seconds"] = results[owner].get("_seconds", 0) + elapsed()

    # platform playbooks need all four platform apartments loaded
    if not only or any(name.startswith("platform_") for name in wanted):
        for name in ("platform_whatsapp", "platform_linkedin",
                     "platform_facebook", "platform_instagram"):
            if name not in apartments:
                try:
                    apartments[name] = _apartment(name)
                except Exception:
                    pass
        try:
            stats = PlatformAgentTrainer().train(apartments)
            results["platform_agents"] = stats
            if not quiet:
                print("  %-22s %-18s ok  %s" % ("platforms", "playbooks",
                                                _brief(stats)))
        except Exception as exc:
            results["platform_agents"] = {"error": str(exc)[:200]}
            if not quiet:
                print("  %-22s %-18s FAILED %s" % ("platforms", "playbooks", exc))

    if verify:
        results["_verify"] = verify_models(apartments, quiet=quiet)

    results["_seconds_total"] = round(time.time() - started, 2)
    return results


def verify_models(apartments: Dict[str, Any], quiet: bool = False) -> Dict[str, Any]:
    """Load every model back and make sure it is readable, JSON, non-empty."""
    report: Dict[str, Any] = {}
    for name, apartment in apartments.items():
        models = apartment.model_names()
        detail = {}
        for model_name in models:
            try:
                payload = apartment.load_model(model_name, default={}) or {}
                detail[model_name] = "ok (%d keys)" % (
                    len(payload) if isinstance(payload, dict) else 1)
            except Exception as exc:
                detail[model_name] = "CORRUPT: %s" % exc
        report[name] = detail
        if not quiet:
            print("  %-22s %d model(s) reloaded" % ("apt_" + name, len(models)))
    return report


def _brief(stats: Any) -> str:
    if isinstance(stats, dict):
        return ", ".join("%s=%s" % (key, value)
                         for key, value in list(stats.items())[:4])
    return str(stats)[:80]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factory.train_all",
                                     description="Train every model from local JSON.")
    parser.add_argument("--force", action="store_true", help="retrain existing models")
    parser.add_argument("--only", help="comma separated apartment names")
    parser.add_argument("--verify", action="store_true",
                        help="reload every model and check it parses")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    only = [part.strip() for part in (args.only or "").split(",") if part.strip()]
    if not args.quiet:
        print("Training ClientHunter Factory from factory/data/*.json")
        print("-" * 72)
    results = run(only=only or None, force=args.force, verify=args.verify,
                  quiet=args.quiet)
    failures = {name: detail for name, detail in results.items()
                if isinstance(detail, dict) and
                any(isinstance(v, dict) and v.get("error") for v in detail.values())}
    if not args.quiet:
        print("-" * 72)
        print("Done in %ss. Failures: %d" % (results.get("_seconds_total"), len(failures)))
        for name, detail in failures.items():
            print("  %s: %s" % (name, detail))
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
