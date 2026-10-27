"""Shared machine-learning primitives - pure Python, JSON-serialisable.

Three models power the whole factory, and all three are small enough to train
on a 4 GB CPU laptop in under a second:

``TfIdfIndex``
    Inverted index + IDF + cosine similarity over the agency knowledge base.
    This is the retrieval half of apt_knowledge's brain (the "R" in RAG, minus
    the external LLM).

``LogisticRegression``
    Batch gradient descent with L2 regularisation and feature standardisation.
    Trains the LeadQualifier from ``data/leads_history.json``.

``MovingAverage``
    One-line online learner used everywhere for "did this tactic work?" -
    objection responses, follow-up days, category conversion rates.

Every model exposes ``to_dict``/``from_dict`` so it can be saved as JSON.  No
pickle anywhere in this codebase: unpickling is arbitrary code execution, and a
factory that trains on data scraped from the internet must never load one.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_TOKEN = re.compile(r"[a-z0-9\u0600-\u06FF]+")


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens, keeping Urdu script and digits."""
    return _TOKEN.findall((text or "").lower())


# --------------------------------------------------------------------------- #
# TF-IDF
# --------------------------------------------------------------------------- #

class TfIdfIndex:
    """BM25-lite: inverted index, IDF, cosine ranking.  JSON in, JSON out."""

    VERSION = "1.1"

    def __init__(self, min_df: int = 1):
        self.min_df = int(min_df)
        self.docs: List[Dict[str, Any]] = []
        self.vocab: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self.index: Dict[str, List[Tuple[int, float]]] = {}
        self.norms: List[float] = []

    # ------------------------------------------------------------------ train
    def fit(self, documents: Iterable[Dict[str, Any]]) -> "TfIdfIndex":
        """``documents``: dicts with ``id``, ``title``, ``body``, optional ``tags``."""
        self.docs = [dict(doc) for doc in documents]
        if not self.docs:
            raise ValueError("no documents to index")

        texts = []
        for doc in self.docs:
            tags = doc.get("tags") or []
            tag_text = " ".join(str(t) for t in tags)
            # tags are repeated so a tag hit outweighs a passing mention
            texts.append("%s %s %s %s" % (doc.get("title", ""), tag_text, tag_text,
                                          doc.get("body", "")))

        tokenised = [tokenize(text) for text in texts]
        doc_freq: Counter = Counter()
        for tokens in tokenised:
            doc_freq.update(set(tokens))
        total = len(tokenised)
        self.vocab = {term: i for i, (term, df) in enumerate(
            sorted((t, df) for t, df in doc_freq.items() if df >= self.min_df))}
        self.idf = {term: math.log((total + 1) / (df + 0.5)) + 1.0
                    for term, df in doc_freq.items() if term in self.vocab}

        self.index = {}
        self.norms = []
        for doc_id, tokens in enumerate(tokenised):
            counts = Counter(t for t in tokens if t in self.vocab)
            if not counts:
                self.norms.append(1.0)
                continue
            max_tf = max(counts.values())
            norm = 0.0
            for term, count in counts.items():
                tf = 0.5 + 0.5 * (count / max_tf)
                weight = tf * self.idf.get(term, 1.0)
                norm += weight * weight
                self.index.setdefault(term, []).append((doc_id, weight))
            norm = math.sqrt(norm) or 1.0
            self.norms.append(norm)
            for term in counts:
                postings = self.index[term]
                postings[-1] = (postings[-1][0], postings[-1][1] / norm)
        return self

    # ----------------------------------------------------------------- search
    def query(self, text: str, top_k: int = 3, min_score: float = 0.02
              ) -> List[Dict[str, Any]]:
        tokens = [t for t in tokenize(text) if t in self.vocab]
        if not tokens:
            return []
        counts = Counter(tokens)
        scores: Dict[int, float] = {}
        for term, count in counts.items():
            weight = (1 + math.log(count)) * self.idf.get(term, 1.0)
            for doc_id, doc_weight in self.index.get(term, []):
                scores[doc_id] = scores.get(doc_id, 0.0) + weight * doc_weight
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out = []
        for doc_id, score in ranked[:top_k]:
            if score < min_score:
                continue
            doc = dict(self.docs[doc_id])
            doc["score"] = round(score, 4)
            out.append(doc)
        return out

    def explain(self, text: str, top_k: int = 3) -> List[Tuple[str, int]]:
        """Which query terms actually matched - useful for debugging retrieval."""
        tokens = set(tokenize(text))
        hits = []
        for term in tokens:
            postings = self.index.get(term)
            if postings:
                hits.append((term, len(postings)))
        hits.sort(key=lambda kv: -kv[1])
        return hits[:top_k]

    # -------------------------------------------------------------- serialise
    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "tfidf_index",
            "version": self.VERSION,
            "min_df": self.min_df,
            "vocab_size": len(self.vocab),
            "documents": [{"id": d.get("id", ""), "title": d.get("title", "")}
                          for d in self.docs],
            "idf": self.idf,
            "index": {term: postings for term, postings in self.index.items()},
            "norms": self.norms,
        }

    def to_full_dict(self) -> Dict[str, Any]:
        """Includes the document text, so a reloaded index can answer queries."""
        data = self.to_dict()
        data["documents"] = self.docs
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TfIdfIndex":
        obj = cls(min_df=int(data.get("min_df", 1)))
        obj.docs = list(data.get("documents") or [])
        obj.idf = dict(data.get("idf") or {})
        obj.index = {term: [tuple(p) for p in postings]
                     for term, postings in (data.get("index") or {}).items()}
        obj.norms = [float(n) for n in (data.get("norms") or [])]
        obj.vocab = {term: i for i, term in enumerate(obj.idf)}
        return obj


# --------------------------------------------------------------------------- #
# Logistic regression
# --------------------------------------------------------------------------- #

class LogisticRegression:
    """Binary logistic regression, batch gradient descent, L2 regularised.

    Features are standardised internally, so raw inputs of wildly different
    scales (reviews in the hundreds, rating 0-5) all behave.
    """

    VERSION = "1.0"

    def __init__(self, learning_rate: float = 0.1, epochs: int = 600,
                 l2: float = 1e-3, threshold: float = 0.5):
        self.learning_rate = float(learning_rate)
        self.epochs = int(epochs)
        self.l2 = float(l2)
        self.threshold = float(threshold)
        self.weights: List[float] = []
        self.bias = 0.0
        self.mean: List[float] = []
        self.std: List[float] = []
        self.feature_names: List[str] = []
        self.loss_history: List[float] = []
        self.n_train = 0

    # ------------------------------------------------------------------ train
    def fit(self, X: Sequence[Sequence[float]], y: Sequence[int],
            feature_names: Optional[Sequence[str]] = None) -> "LogisticRegression":
        rows = [list(map(float, row)) for row in X]
        labels = [int(v) for v in y]
        if not rows or len(rows) != len(labels):
            raise ValueError("X and y must be non-empty and the same length")
        n_features = len(rows[0])
        self.feature_names = list(feature_names or
                                  ["f%d" % i for i in range(n_features)])
        self.n_train = len(rows)

        # standardise
        self.mean = [sum(row[i] for row in rows) / len(rows) for i in range(n_features)]
        variance = [sum((row[i] - self.mean[i]) ** 2 for row in rows) / len(rows)
                    for i in range(n_features)]
        self.std = [math.sqrt(v) if v > 1e-12 else 1.0 for v in variance]
        scaled = [[(row[i] - self.mean[i]) / self.std[i] for i in range(n_features)]
                  for row in rows]

        self.weights = [0.0] * n_features
        self.bias = 0.0
        n = len(scaled)
        for epoch in range(self.epochs):
            grad_w = [0.0] * n_features
            grad_b = 0.0
            loss = 0.0
            for row, label in zip(scaled, labels):
                z = self.bias + sum(w * x for w, x in zip(self.weights, row))
                z = max(-30.0, min(30.0, z))
                p = 1.0 / (1.0 + math.exp(-z))
                error = p - label
                for i in range(n_features):
                    grad_w[i] += error * row[i]
                grad_b += error
                # numerically stable log loss
                loss += -(label * _log(p) + (1 - label) * _log(1 - p))
            for i in range(n_features):
                grad_w[i] = grad_w[i] / n + self.l2 * self.weights[i]
                self.weights[i] -= self.learning_rate * grad_w[i]
            self.bias -= self.learning_rate * (grad_b / n)
            loss = loss / n + 0.5 * self.l2 * sum(w * w for w in self.weights)
            if epoch % 50 == 0 or epoch == self.epochs - 1:
                self.loss_history.append(round(loss, 5))
        return self

    # ---------------------------------------------------------------- predict
    def _scale(self, row: Sequence[float]) -> List[float]:
        return [((row[i] if i < len(row) else 0.0) - self.mean[i]) / self.std[i]
                for i in range(len(self.weights))]

    def predict_proba(self, row: Sequence[float]) -> float:
        if not self.weights:
            return 0.5
        z = self.bias + sum(w * x for w, x in zip(self.weights, self._scale(row)))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def predict(self, row: Sequence[float]) -> int:
        return 1 if self.predict_proba(row) >= self.threshold else 0

    def score(self, X: Sequence[Sequence[float]], y: Sequence[int]) -> float:
        correct = sum(1 for row, label in zip(X, y) if self.predict(row) == int(label))
        return correct / max(len(list(y)), 1)

    def top_features(self, n: int = 6) -> List[Tuple[str, float]]:
        """Most influential features - the model explaining itself to you."""
        pairs = list(zip(self.feature_names, self.weights))
        pairs.sort(key=lambda kv: -abs(kv[1]))
        return pairs[:n]

    # -------------------------------------------------------------- serialise
    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "logistic_regression",
            "version": self.VERSION,
            "weights": self.weights,
            "bias": self.bias,
            "mean": self.mean,
            "std": self.std,
            "feature_names": self.feature_names,
            "threshold": self.threshold,
            "n_train": self.n_train,
            "final_loss": self.loss_history[-1] if self.loss_history else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LogisticRegression":
        obj = cls(threshold=float(data.get("threshold", 0.5)))
        obj.weights = [float(w) for w in (data.get("weights") or [])]
        obj.bias = float(data.get("bias", 0.0))
        obj.mean = [float(m) for m in (data.get("mean") or [])]
        obj.std = [float(s) for s in (data.get("std") or [])]
        obj.feature_names = list(data.get("feature_names") or [])
        obj.n_train = int(data.get("n_train", 0))
        return obj


def _log(value: float) -> float:
    return math.log(value) if value > 1e-12 else -20.0


# --------------------------------------------------------------------------- #
# Online averaging
# --------------------------------------------------------------------------- #

class MovingAverage:
    """Exponentially-weighted success rate for a tactic (0..1)."""

    def __init__(self, rate: float = 0.25, default: float = 0.5):
        self.rate = float(rate)
        self.default = float(default)

    def update(self, current: float, success: bool) -> float:
        target = 1.0 if success else 0.0
        return round(current + self.rate * (target - current), 4)

    def blend(self, current: float, observed: float, weight: float = 0.3) -> float:
        return round((1 - weight) * current + weight * observed, 4)


def train_test_split(rows: Sequence[Any], ratio: float = 0.8, seed: int = 7):
    """Deterministic split without numpy."""
    import random
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    cut = int(len(shuffled) * ratio)
    return shuffled[:cut], shuffled[cut:]
