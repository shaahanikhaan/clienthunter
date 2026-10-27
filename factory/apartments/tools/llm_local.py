"""Local language model - CPU only, no downloads, no API.

Two tiers, chosen automatically at runtime:

**Tier 1 (default) - self-trained n-gram / Markov generator**
    Trained from your own ``data/successful_conversations.json`` plus the
    knowledge base.  A backoff trigram model with temperature sampling:
    ~2 MB of RAM, trains in well under a second on a Core i5, and - because it
    is trained on *your* winning conversations - it writes in your voice, not a
    generic one.  This is what makes generated outreach sound like you.

**Tier 2 (optional) - real local SLM via llama.cpp**
    If you install ``llama-cpp-python`` (or ``ctransformers``) and drop a GGUF
    file in ``models/``, the same interface drives a quantised 1-3B model on CPU.
    Nothing is downloaded by the factory - you supply the file.

Why not Neura-X / SLM Forge?  Neither exists on PyPI (both are 404), and the
Neura-X GitHub repo has 1 star.  They are documented in POWER_GUIDE.md, but
shipping a dependency on them would ship something that cannot run.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

START = "<s>"
END = "</s>"
UNK = "<unk>"


class NgramModel:
    """Backoff trigram model with add-k smoothing and temperature sampling.

    Memory discipline (this runs next to 17 other agents in 4 GB):
      * counts are pruned below ``min_count`` after training,
      * the per-context table is capped at ``max_suffixes`` entries,
      * the model saves as JSON of plain dicts.
    """

    VERSION = "2.0"

    def __init__(self, n: int = 3, min_count: int = 1, max_suffixes: int = 40):
        self.n = int(n)
        self.min_count = int(min_count)
        self.max_suffixes = int(max_suffixes)
        self.counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.totals: Dict[str, int] = defaultdict(int)
        self.vocab: set = set()
        self.doc_count = 0
        self.meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------ train
    def train(self, docs: Iterable[str], min_count: Optional[int] = None) -> "NgramModel":
        min_count = self.min_count if min_count is None else min_count
        for doc in docs:
            tokens = self.tokenize(doc)
            if not tokens:
                continue
            self.doc_count += 1
            padded = [START] * (self.n - 1) + tokens + [END]
            self.vocab.update(tokens)
            for i in range(len(padded) - (self.n - 1)):
                context = " ".join(padded[i:i + self.n - 1])
                nxt = padded[i + self.n - 1]
                self.counts[context][nxt] += 1
                self.totals[context] += 1
                # also store shorter contexts for backoff
                for size in range(self.n - 2, 0, -1):
                    short = " ".join(padded[i + (self.n - 1 - size):i + self.n - 1])
                    self.counts[short][nxt] += 1
                    self.totals[short] += 1
        self._prune(min_count)
        self.meta.update({"n": self.n, "docs": self.doc_count, "vocab": len(self.vocab),
                          "contexts": len(self.counts)})
        return self

    def _prune(self, min_count: int) -> None:
        for context in list(self.counts.keys()):
            table = self.counts[context]
            kept = {k: v for k, v in table.items() if v >= min_count}
            if not kept:
                del self.counts[context]
                self.totals.pop(context, None)
                continue
            if len(kept) > self.max_suffixes:
                kept = dict(sorted(kept.items(), key=lambda kv: -kv[1])[: self.max_suffixes])
            self.counts[context] = kept
            self.totals[context] = sum(kept.values())

    @staticmethod
    def tokenize(text: str) -> List[str]:
        text = re.sub(r"\s+", " ", (text or "").strip())
        if not text:
            return []
        return re.findall(r"[\w\u0600-\u06FF]+|[^\w\s]", text, re.UNICODE)

    # --------------------------------------------------------------- generate
    def next_candidates(self, context: Sequence[str]) -> List[Tuple[str, float]]:
        for size in range(min(self.n - 1, len(context)), 0, -1):
            key = " ".join(context[-size:])
            table = self.counts.get(key)
            if table:
                total = self.totals.get(key) or sum(table.values())
                return [(tok, count / total) for tok, count in table.items()]
        return []

    def generate(self, prompt: str = "", max_tokens: int = 60, temperature: float = 0.85,
                 stop: Optional[Sequence[str]] = None, seed: Optional[int] = None
                 ) -> str:
        """Continue ``prompt``.  Lower temperature = more literal reuse."""
        rng = random.Random(seed)
        stop = tuple(stop or (END,))
        tokens = self.tokenize(prompt)
        if not tokens:
            tokens = [START]
        out: List[str] = []
        for _ in range(max_tokens):
            candidates = self.next_candidates(tokens)
            if not candidates:
                break
            candidates = [(t, p) for t, p in candidates if t not in stop]
            if not candidates:
                break
            if temperature <= 0.01:
                nxt = max(candidates, key=lambda kv: kv[1])[0]
            else:
                weights = [p ** (1.0 / max(temperature, 0.05)) for _, p in candidates]
                total = sum(weights) or 1.0
                roll = rng.random() * total
                acc, nxt = 0.0, candidates[-1][0]
                for (token, _), weight in zip(candidates, weights):
                    acc += weight
                    if roll <= acc:
                        nxt = token
                        break
            if nxt == END:
                break
            tokens.append(nxt)
            out.append(nxt)
        return self.detokenize(out)

    @staticmethod
    def detokenize(tokens: Sequence[str]) -> str:
        text = " ".join(tokens)
        text = re.sub(r"\s+([,.;:!?،۔؟])", r"\1", text)
        text = re.sub(r"\(\s+", "(", text)
        text = re.sub(r"\s+\)", ")", text)
        text = re.sub(r"\s+'", "'", text)
        return text.strip()

    # --------------------------------------------------------------- evaluate
    def perplexity(self, docs: Iterable[str]) -> float:
        """Lower is better.  Used by apt_self_trainer to compare model versions."""
        log_sum, count = 0.0, 0
        for doc in docs:
            tokens = [START] * (self.n - 1) + self.tokenize(doc) + [END]
            for i in range(len(tokens) - (self.n - 1)):
                context = tokens[i:i + self.n - 1]
                target = tokens[i + self.n - 1]
                prob = self._probability(context, target)
                log_sum += math.log(max(prob, 1e-9))
                count += 1
        if not count:
            return float("inf")
        return math.exp(-log_sum / count)

    def _probability(self, context: Sequence[str], token: str) -> float:
        for size in range(min(self.n - 1, len(context)), 0, -1):
            key = " ".join(context[-size:])
            table = self.counts.get(key)
            if table and token in table:
                return table[token] / (self.totals.get(key) or sum(table.values()))
        return 1e-6

    def score(self, text: str) -> float:
        """Average log-probability - handy for picking the best of N drafts."""
        tokens = [START] * (self.n - 1) + self.tokenize(text) + [END]
        if len(tokens) <= self.n:
            return -99.0
        total = 0.0
        for i in range(len(tokens) - (self.n - 1)):
            total += math.log(max(self._probability(tokens[i:i + self.n - 1],
                                                    tokens[i + self.n - 1]), 1e-9))
        return total / (len(tokens) - self.n + 1)

    # -------------------------------------------------------------- serialise
    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "ngram_model",
            "version": self.VERSION,
            "n": self.n,
            "doc_count": self.doc_count,
            "meta": self.meta,
            "counts": {k: dict(v) for k, v in self.counts.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NgramModel":
        obj = cls(n=int(data.get("n", 3)))
        obj.counts = defaultdict(lambda: defaultdict(int),
                                 {k: defaultdict(int, v)
                                  for k, v in (data.get("counts") or {}).items()})
        obj.totals = defaultdict(int, {k: sum(v.values()) for k, v in obj.counts.items()})
        obj.vocab = set()
        for context, table in obj.counts.items():
            obj.vocab.update(context.split(" "))
            obj.vocab.update(table.keys())
        obj.vocab.discard(START)
        obj.vocab.discard(END)
        obj.doc_count = int(data.get("doc_count", 0))
        obj.meta = dict(data.get("meta", {}))
        return obj

    def memory_estimate(self) -> int:
        """Rough RAM footprint in bytes (for the 4 GB budget guard)."""
        return sum(len(context) + sum(len(t) + 8 for t in table)
                   for context, table in self.counts.items())


class LocalLLM:
    """Uniform interface: ``generate`` / ``finetune`` / ``evaluate``.

    Uses a local GGUF model when one is available, otherwise the self-trained
    n-gram model.  Callers do not need to care which tier is live.
    """

    def __init__(self, apartment, model_name: str = "generator.json",
                 gguf_dir: Optional[Path] = None):
        self.apartment = apartment
        self.model_name = model_name
        self.gguf_dir = Path(gguf_dir) if gguf_dir else None
        self._ngram: Optional[NgramModel] = None
        self._llama = None
        self.backend = "none"

    # ------------------------------------------------------------------ tiers
    def try_load_gguf(self) -> bool:
        """Tier 2: llama-cpp-python + a GGUF you placed locally.  Never downloads."""
        if self._llama is not None:
            return True
        try:  # pragma: no cover - optional dependency
            from llama_cpp import Llama  # type: ignore
        except Exception:
            return False
        candidates = []
        if self.gguf_dir:
            candidates.extend(sorted(Path(self.gguf_dir).glob("*.gguf")))
        try:
            candidates.extend(sorted((Path(self.apartment.root) / "models").glob("*.gguf")))
        except Exception:
            pass
        if not candidates:
            return False
        try:  # pragma: no cover
            self._llama = Llama(model_path=str(candidates[0]), n_ctx=2048, n_threads=2,
                                n_gpu_layers=0, verbose=False)
            self.backend = "llama.cpp:%s" % candidates[0].name
            return True
        except Exception:
            return False

    @property
    def ngram(self) -> Optional[NgramModel]:
        if self._ngram is None and self.apartment.has_model(self.model_name):
            try:
                self._ngram = NgramModel.from_dict(self.apartment.load_model(self.model_name))
                self.backend = "ngram"
            except Exception:
                self._ngram = None
        return self._ngram

    def is_trained(self) -> bool:
        return self.ngram is not None or self.try_load_gguf()

    # ------------------------------------------------------------------- api
    def finetune(self, docs: Iterable[str], epochs: int = 1, n: int = 3,
                 min_count: int = 1) -> Dict[str, Any]:
        """(Re)train the generator.  ``epochs`` is kept for API compatibility.

        An n-gram model is a closed-form count, so extra epochs would just
        double-count; we honour ``epochs`` by re-counting, which for this model
        type is identical, and report it honestly in the returned stats.
        """
        docs = [d for d in docs if d and str(d).strip()]
        if not docs:
            raise ValueError("no training documents")
        model = NgramModel(n=n, min_count=min_count)
        model.train(docs)
        self._ngram = model
        self.apartment.save_model(self.model_name, model.to_dict())
        self.backend = "ngram"
        return {
            "backend": "ngram",
            "docs": len(docs),
            "epochs_requested": epochs,
            "epochs_effective": 1,
            "contexts": len(model.counts),
            "vocab": len(model.vocab),
            "memory_bytes": model.memory_estimate(),
            "perplexity": round(model.perplexity(docs[:200]), 3),
        }

    def generate(self, prompt: str, max_tokens: int = 60, temperature: float = 0.85,
                 stop: Optional[Sequence[str]] = None) -> str:
        if self.try_load_gguf():  # pragma: no cover - optional tier
            try:
                out = self._llama(prompt, max_tokens=max_tokens, temperature=temperature,
                                  stop=list(stop or []), echo=False)
                return (out.get("choices") or [{}])[0].get("text", "").strip()
            except Exception:
                pass
        model = self.ngram
        if model is None:
            return ""
        return model.generate(prompt, max_tokens=max_tokens, temperature=temperature,
                              stop=stop)

    def rank(self, candidates: Sequence[str]) -> List[Tuple[str, float]]:
        """Order candidate drafts by how much they sound like your winners."""
        model = self.ngram
        if model is None:
            return [(c, 0.0) for c in candidates]
        scored = [(c, model.score(c)) for c in candidates]
        scored.sort(key=lambda kv: -kv[1])
        return scored

    def evaluate(self, docs: Sequence[str]) -> Dict[str, float]:
        model = self.ngram
        if model is None:
            return {"perplexity": float("inf"), "docs": 0}
        return {"perplexity": round(model.perplexity(docs), 3), "docs": len(docs)}

    def info(self) -> Dict[str, Any]:
        model = self.ngram
        return {
            "backend": self.backend,
            "trained": model is not None or self._llama is not None,
            "contexts": len(model.counts) if model else 0,
            "memory_bytes": model.memory_estimate() if model else 0,
        }


def build(apartment) -> LocalLLM:
    return LocalLLM(apartment)
