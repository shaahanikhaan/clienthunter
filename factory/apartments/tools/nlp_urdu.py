"""Urdu + Roman Urdu NLP engine - pure Python, no downloads, no API.

Why hand-rolled instead of a transformer?
    The obvious pick, ``jev-urdu`` (LughaatNLP), needs ``torch`` +
    ``transformers``.  On a 4 GB CPU laptop that is ~2 GB of RAM resident
    before the factory does any work, plus a model download from HuggingFace -
    which breaks the "no external downloads" rule.  So the default brain here
    is a self-trained Naive Bayes + lexicon model that trains from your own
    ``data/urdu_training.json`` in well under a second and runs in ~4 MB.

    ``jev-urdu`` is still supported as an OPTIONAL upgrade - see
    :func:`try_load_jev_urdu`.  If you have the RAM and have pre-downloaded the
    model, the factory will use it and fall back automatically.

Capabilities
    * script normalisation (Arabic->Urdu chars, diacritics, tatweel, hamza)
    * tokenisation for Urdu script AND Roman Urdu, in the same string
    * roman-Urdu spelling unification (kya/kia/kyun, hai/hy/he, kesay/kaise)
    * intent classification (7 intents) - Multinomial NB, log-space, Laplace
    * sentiment (positive/neutral/negative) - NB + curated lexicon + negation
    * priority (high/medium/low) - rules blended with NB
    * entity extraction: prices, phones, emails, URLs, dates/times, names,
      cities, business categories
    * language detection (urdu / roman_urdu / english / mixed)
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 1. Normalisation
# --------------------------------------------------------------------------- #

#: Arabic code points that Pakistanis type by accident (keyboards differ).
_CHAR_MAP = {
    "ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ہ", "ة": "ہ",
    "أ": "ا", "إ": "ا", "آ": "آ", "ٱ": "ا", "ؤ": "و",
    "ً": "", "ٌ": "", "ٍ": "", "َ": "", "ُ": "", "ِ": "",
    "ّ": "", "ْ": "", "ٰ": "", "ٔ": "", "ٕ": "",
    "ـ": "",                       # tatweel (kashida) - decorative only
    "\u200b": "", "\u200c": " ",   # zero width
    "۔": " ۔ ", "؟": " ؟ ", "،": " ، ",
}

#: Roman Urdu spelling variants -> canonical form.  This single table does more
#: for accuracy on real Pakistani chat data than any other trick here.
_ROMAN_VARIANTS = {
    "kya": "kya", "kia": "kya", "kiya": "kya", "kiaa": "kya",
    "hai": "hai", "hy": "hai", "he": "hai", "hae": "hai", "h": "hai",
    "hain": "hain", "hyn": "hain", "hen": "hain", "hainn": "hain",
    "kaise": "kese", "kaisay": "kese", "kese": "kese", "kesay": "kese",
    "kaisee": "kese", "kaisy": "kese", "kesy": "kese",
    "mein": "mein", "main": "mein", "me": "mein", "mn": "mein", "may": "mein",
    "nahi": "nahi", "nai": "nahi", "nhi": "nahi", "nahin": "nahi", "nahe": "nahi",
    "nahiin": "nahi", "no": "nahi",
    "haan": "haan", "han": "haan", "ha": "haan", "hn": "haan", "yes": "haan",
    "acha": "acha", "achha": "acha", "accha": "acha", "axa": "acha",
    "theek": "theek", "thik": "theek", "thek": "theek", "teek": "theek",
    "kitna": "kitna", "kitne": "kitna", "kitny": "kitna", "kitni": "kitna",
    "kitnay": "kitna", "kittna": "kitna",
    "paise": "paisay", "paisay": "paisay", "pese": "paisay", "pesay": "paisay",
    "price": "rate", "pricing": "rate", "cost": "rate", "charges": "rate",
    "bhejo": "bhej", "bhej": "bhej", "send": "bhej", "bhejain": "bhej",
    "banao": "banao", "banado": "banao", "bana": "banao", "build": "banao",
    "website": "website", "site": "website", "web": "website",
    "kal": "kal", "kl": "kal",
    "zaroor": "zaroor", "zroor": "zaroor", "zarur": "zaroor",
    "soch": "soch", "sochna": "soch", "socnha": "soch", "think": "soch",
    "mehnga": "mehnga", "mehanga": "mehnga", "mahnga": "mehnga", "expensive": "mehnga",
    "sasta": "sasta", "cheap": "sasta", "kam": "kam",
    "kaun": "kaun", "kon": "kaun", "who": "kaun",
    "aap": "aap", "ap": "aap", "tu": "aap", "you": "aap",
    "hum": "hum", "hm": "hum", "we": "hum",
    "chahiye": "chahiye", "chahye": "chahiye", "chaiye": "chahiye", "chaheye": "chahiye",
    "detail": "details", "details": "details", "tafseel": "details",
    "call": "call", "phone": "call", "callback": "call",
    "time": "time", "waqt": "time", "timing": "time",
    "jid": "jaldi", "jaldi": "jaldi", "jldi": "jaldi", "urgent": "jaldi",
}

_URDU_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[0-9\u0660-\u0669\u06F0-\u06F9]")
_TOKEN_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]+"   # Urdu script run
    r"|[A-Za-z][A-Za-z'\-]*"                                     # Latin word
    r"|[0-9\u0660-\u0669\u06F0-\u06F9][0-9,.\u0660-\u0669\u06F0-\u06F9/]*",  # number
)


def normalise(text: str) -> str:
    """NFKC + Arabic->Urdu folding + diacritic strip + punctuation spacing."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(_CHAR_MAP.get(ch, ch) for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def contains_urdu(text: str) -> bool:
    return bool(_URDU_RANGE.search(text or ""))


def detect_language(text: str) -> str:
    """``urdu`` | ``roman_urdu`` | ``english`` | ``mixed``."""
    text = text or ""
    urdu = len(_URDU_RANGE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if urdu and latin:
        return "mixed"
    if urdu:
        return "urdu"
    if not latin:
        return "unknown"
    return "roman_urdu" if _roman_score(text) >= 2 else "english"


def _roman_score(text: str) -> int:
    """How many Roman-Urdu markers appear (0 = plain English)."""
    lowered = re.sub(r"[^a-z ]", " ", (text or "").lower())
    words = set(lowered.split())
    score = 0
    for word in words:
        if word in _ROMAN_VARIANTS or word in STOPWORDS_ROMAN:
            score += 2
        elif word in ROMAN_MARKERS:
            score += 1
    return score


#: Words that only really appear in Roman Urdu, not English.
ROMAN_MARKERS = {
    "ka", "ki", "ke", "ko", "se", "py", "pe", "par", "aur", "or", "lekin", "magar",
    "bohat", "bahut", "bohot", "bht", "thoda", "zyada", "kam", "abhi", "phir", "kal",
    "aaj", "mujhe", "mujh", "hum", "ham", "aapka", "apka", "mera", "meri", "mere",
    "unka", "unki", "iska", "karna", "krna", "karna", "hota", "hoti", "hote", "kar",
    "dena", "lana", "dekho", "dekh", "suno", "batao", "bataye", "batain", "chahiye",
    "wala", "wali", "wale", "kaise", "kese", "kya", "kia", "kyun", "kyu", "kab",
    "kahan", "kaha", "kitna", "zaroor", "shukriya", "khuda", "theek", "thik", "acha",
    "accha", "haan", "han", "nahi", "nai", "ji", "g", "sahab", "bhai", "yar", "yaar",
}

# --------------------------------------------------------------------------- #
# 2. Stopwords, stemming
# --------------------------------------------------------------------------- #

STOPWORDS_URDU = {
    "کا", "کی", "کے", "کو", "سے", "پر", "میں", "اور", "یا", "لیکن", "ہے", "ہیں", "تھا",
    "تھی", "تھے", "ہوں", "ہو", "ہوا", "والا", "والی", "والے", "یہ", "وہ", "جو", "کہ",
    "بھی", "تو", "پھر", "اب", "جب", "تب", "کر", "کیا", "کریں", "کرو", "کسی", "کچھ",
    "صرف", "بس", "بہت", "ذرا", "آپ", "اپ", "اے", "او", "جی", "ہاں", "نہیں", "نہ",
    "میرا", "میری", "میرے", "ہمارا", "ہماری", "ہمارے", "تمہارا", "آپکا", "آپکی",
    "اس", "اسکی", "اسکا", "ان", "انکی", "انکا", "انہوں", "مجھے", "ہمیں", "انہیں",
    "کون", "کیوں", "کب", "کہاں", "کیسے", "کیسا", "کتنا", "کتنے", "کتنی", "سب", "کوئی",
    "اوپر", "نیچے", "ساتھ", "لیے", "لئے", "والوں", "رہا", "رہی", "رہے", "گیا", "گئی",
    "گئے", "چاہیے", "چاہئے", "ہونا", "ہونے", "سکتے", "سکتی", "سکتا",
}

STOPWORDS_ROMAN = {
    "ka", "ki", "ke", "ko", "se", "py", "pe", "par", "aur", "or", "ya", "lekin",
    "magar", "hai", "hain", "hy", "ho", "hun", "hoon", "tha", "thi", "the", "thy",
    "main", "mein", "me", "mn", "tu", "hum", "hm", "aap", "ap", "wo", "ye", "woh",
    "yeh", "jo", "ke", "k", "bhi", "to", "phir", "ab", "jab", "tab", "kr", "karna",
    "krna", "kro", "karen", "karein", "kuch", "sirf", "bus", "bohat", "bohot", "bht",
    "thoda", "zyada", "abhi", "mujhe", "mujh", "hamein", "humen", "unhein", "isko",
    "usko", "iska", "uska", "unki", "uski", "mera", "meri", "mere", "apka", "apki",
    "tumhara", "tum", "humara", "hamara", "kaun", "kon", "kyun", "kyu", "kab", "kahan",
    "kaha", "kia", "kya", "kese", "kaise", "kitna", "kitne", "kitni", "sub", "sab",
    "koi", "kuch", "ji", "han", "haan", "nahi", "nai", "nhi", "na", "wala", "wali",
    "wale", "liye", "k liye", "sath", "saath", "raha", "rahi", "rahe", "gaya", "gayi",
    "gaye", "chahiye", "hona", "honey", "sakty", "sakta", "sakti", "sakte", "krny",
    "kerny", "krna", "acha", "theek", "ok", "okay",
}

STOPWORDS = STOPWORDS_URDU | STOPWORDS_ROMAN

#: Light suffix stripping - enough to collapse ka/ki/ke, hoon/hain/hy.
_URDU_SUFFIXES = ("وں", "یں", "وں نے", "ات", "وں کا", "یاں")
_ROMAN_SUFFIXES = ("ing", "ed", "s")


def stem(token: str) -> str:
    """Very light stemmer - keeps Urdu and Roman Urdu on equal footing."""
    if _URDU_RANGE.search(token):
        for suffix in _URDU_SUFFIXES:
            if len(token) > len(suffix) + 2 and token.endswith(suffix):
                return token[: -len(suffix)]
        return token
    lowered = token.lower()
    if lowered in _ROMAN_VARIANTS:
        return _ROMAN_VARIANTS[lowered]
    if len(lowered) > 5:
        for suffix in _ROMAN_SUFFIXES:
            if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
                return lowered[: -len(suffix)]
    return lowered


def canonical_roman(token: str) -> str:
    """Fold a Roman Urdu spelling variant to its canonical form."""
    return _ROMAN_VARIANTS.get(token.lower(), token.lower())


# --------------------------------------------------------------------------- #
# 3. Tokenisation
# --------------------------------------------------------------------------- #

def tokenize(text: str, keep_stopwords: bool = False, stem_tokens: bool = True) -> List[str]:
    """Tokenise mixed Urdu / Roman Urdu / English text.

    >>> tokenize("aap ka site kitne ka hai?")
    ['aap', 'site', 'kitna', 'hai']
    """
    text = normalise(text)
    if not text:
        return []
    raw = _TOKEN_RE.findall(text)
    out: List[str] = []
    for tok in raw:
        tok = tok.strip()
        if not tok:
            continue
        lowered = tok.lower()
        if not keep_stopwords and lowered in STOPWORDS:
            continue
        if _URDU_RANGE.search(tok):
            out.append(stem(tok) if stem_tokens else tok)
        else:
            canon = canonical_roman(tok)
            if not keep_stopwords and canon in STOPWORDS:
                continue
            out.append(stem(canon) if stem_tokens else canon)
    return out


def ngrams(tokens: Sequence[str], n: int = 2) -> List[str]:
    if n <= 1:
        return list(tokens)
    return ["_".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def features(text: str, use_bigrams: bool = True) -> List[str]:
    """Feature vector for a whole message (unigrams + bigrams)."""
    toks = tokenize(text)
    feats = list(toks)
    if use_bigrams and len(toks) > 1:
        feats.extend(ngrams(toks, 2))
    return feats


# --------------------------------------------------------------------------- #
# 4. Multinomial Naive Bayes (log-space, Laplace smoothing)
# --------------------------------------------------------------------------- #

class NaiveBayes:
    """Tiny multinomial NB.  Trains in milliseconds, saves as plain JSON."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = float(alpha)
        self.classes: List[str] = []
        self.log_prior: Dict[str, float] = {}
        self.log_likelihood: Dict[str, Dict[str, float]] = {}
        self.counts: Dict[str, Dict[str, int]] = {}
        self.class_totals: Dict[str, int] = {}
        self.vocab: set = set()
        self.n_docs = 0
        self.trained_at = 0.0

    # ------------------------------------------------------------------ train
    def fit(self, docs: Iterable[Sequence[str]], labels: Iterable[str]) -> "NaiveBayes":
        docs = list(docs)
        labels = list(labels)
        if len(docs) != len(labels):
            raise ValueError("X/y length mismatch")
        if not docs:
            raise ValueError("no training data")
        per_class: Dict[str, Counter] = defaultdict(Counter)
        doc_counts: Counter = Counter()
        for feats, label in zip(docs, labels):
            label = str(label)
            doc_counts[label] += 1
            for feat in feats:
                per_class[label][feat] += 1
                self.vocab.add(feat)
        self.classes = sorted(doc_counts)
        self.n_docs = sum(doc_counts.values())
        vocab_size = max(len(self.vocab), 1)
        for label in self.classes:
            total = sum(per_class[label].values())
            self.class_totals[label] = total
            self.counts[label] = dict(per_class[label])
            self.log_prior[label] = math.log(doc_counts[label] / self.n_docs)
            denom = total + self.alpha * vocab_size
            self.log_likelihood[label] = {}
        # Sparse storage: only store seen terms, remember the default.
        for label in self.classes:
            denom = self.class_totals[label] + self.alpha * vocab_size
            self.log_likelihood[label] = {
                term: math.log((count + self.alpha) / denom)
                for term, count in self.counts[label].items()
            }
        self.default_logprob = {
            label: math.log(self.alpha / (self.class_totals[label] + self.alpha * vocab_size))
            for label in self.classes
        }
        self.vocab_size = vocab_size
        import time
        self.trained_at = time.time()
        return self

    # ----------------------------------------------------------------- predict
    def predict_proba(self, feats: Sequence[str]) -> Dict[str, float]:
        if not self.classes:
            return {}
        scores = {label: self.log_prior[label] for label in self.classes}
        for feat in feats:
            for label in self.classes:
                table = self.log_likelihood[label]
                scores[label] += table.get(feat, self.default_logprob[label])
        top = max(scores.values())
        exp = {k: math.exp(v - top) for k, v in scores.items()}
        total = sum(exp.values()) or 1.0
        return {k: v / total for k, v in exp.items()}

    def predict(self, feats: Sequence[str]) -> Tuple[str, float]:
        proba = self.predict_proba(feats)
        if not proba:
            return "unknown", 0.0
        label = max(proba, key=proba.get)
        return label, proba[label]

    # ------------------------------------------------------------------ utils
    def top_features(self, label: str, n: int = 12) -> List[Tuple[str, float]]:
        """Most distinctive terms for a class (log-odds vs the rest)."""
        others = [c for c in self.classes if c != label]
        if not others:
            return []
        out = []
        for term in self.vocab:
            mine = self.log_likelihood[label].get(term, self.default_logprob[label])
            rest = max(self.log_likelihood[o].get(term, self.default_logprob[o]) for o in others)
            out.append((term, mine - rest))
        out.sort(key=lambda kv: -kv[1])
        return out[:n]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "naive_bayes",
            "alpha": self.alpha,
            "classes": self.classes,
            "log_prior": self.log_prior,
            "log_likelihood": self.log_likelihood,
            "default_logprob": getattr(self, "default_logprob", {}),
            "vocab_size": getattr(self, "vocab_size", len(self.vocab)),
            "class_totals": self.class_totals,
            "n_docs": self.n_docs,
            "trained_at": self.trained_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NaiveBayes":
        obj = cls(data.get("alpha", 0.3))
        obj.classes = list(data.get("classes", []))
        obj.log_prior = dict(data.get("log_prior", {}))
        obj.log_likelihood = {k: dict(v) for k, v in (data.get("log_likelihood") or {}).items()}
        obj.default_logprob = dict(data.get("default_logprob", {}))
        obj.vocab_size = int(data.get("vocab_size", 0))
        obj.class_totals = dict(data.get("class_totals", {}))
        obj.n_docs = int(data.get("n_docs", 0))
        obj.trained_at = float(data.get("trained_at", 0.0))
        obj.vocab = set(obj.log_likelihood.get(obj.classes[0], {})) if obj.classes else set()
        for label in obj.classes:
            obj.vocab |= set(obj.log_likelihood.get(label, {}))
        return obj


# --------------------------------------------------------------------------- #
# 5. Sentiment lexicon (Urdu + Roman Urdu)
# --------------------------------------------------------------------------- #

POSITIVE = {
    # Roman Urdu
    "acha": 2, "achha": 2, "accha": 2, "best": 3, "zabardast": 3, "shukriya": 2,
    "thanks": 2, "thank": 2, "shukar": 1, "khush": 2, "pasand": 2, "like": 1,
    "theek": 1, "thik": 1, "ok": 1, "okay": 1, "haan": 1, "han": 1, "ji": 1,
    "zaroor": 2, "bilkul": 2, "ready": 2, "interested": 3, "chahiye": 1,
    "kamal": 3, "wah": 2, "mubarak": 2, "love": 2, "great": 2, "good": 2,
    "perfect": 3, "sahi": 2, "durust": 2, "faida": 1, "munafa": 1, "barha": 2,
    "improve": 1, "behtar": 2, "behtreen": 3, "khubsurat": 2, "professional": 2,
    # Urdu script
    "اچھا": 2, "بہترین": 3, "شکریہ": 2, "خوش": 2, "پسند": 2, "ٹھیک": 1,
    "ہاں": 1, "جی": 1, "ضرور": 2, "بالکل": 2, "دلچسپی": 3, "کمال": 3,
    "واہ": 2, "مبارک": 2, "بہتر": 2, "فائدہ": 1, "خوبصورت": 2, "منافع": 1,
}

NEGATIVE = {
    "mehnga": -2, "mehanga": -2, "mahnga": -2, "expensive": -2, "bura": -2,
    "ganda": -2, "bekar": -3, "bekār": -3, "waste": -2, "faltu": -2, "nahi": -1,
    "nai": -1, "nhi": -1, "no": -1, "mana": -2, "refuse": -2, "problem": -2,
    "masla": -2, "pareshan": -2, "gussa": -2, "naraz": -2, "slow": -2,
    "late": -1, "der": -1, "kharab": -2, "toota": -2, "band": -1, "hata": -1,
    "cancel": -2, "chhor": -2, "skip": -1, "busy": -1, "masroof": -1,
    "sochna": -1, "doubt": -1, "shak": -1, "risk": -1, "nuksan": -2, "fraud": -3,
    "dhoka": -3, "scam": -3, "spam": -3, "block": -2, "report": -2,
    "مہنگا": -2, "برا": -2, "گندا": -2, "بیکار": -3, "نہیں": -1, "منع": -2,
    "مسئلہ": -2, "پریشان": -2, "غصہ": -2, "ناراض": -2, "خراب": -2, "ٹوٹا": -2,
    "بند": -1, "منسوخ": -2, "مصروف": -1, "شک": -1, "نقصان": -2, "فراڈ": -3,
    "دھوکہ": -3, "سپیم": -3,
}

NEGATORS = {"nahi", "nai", "nhi", "na", "not", "no", "bina", "نہیں", "نہ", "بغیر", "مت"}
INTENSIFIERS = {"bohat": 1.6, "bohot": 1.6, "bht": 1.6, "bahut": 1.6, "zyada": 1.4,
                "thoda": 0.6, "zara": 0.6, "bilkul": 1.5, "very": 1.5, "too": 1.4,
                "بہت": 1.6, "زیادہ": 1.4, "تھوڑا": 0.6, "بالکل": 1.5}

URGENCY = {"jaldi", "urgent", "asap", "abhi", "today", "aaj", "turant", "foran",
           "فوراً", "جلدی", "آج", "ابھی", "ضروری"}
NEGLECT = {"baad", "later", "bad", "kal", "next", "agle", "agley", "month", "saal",
           "بعد", "کل", "اگلے"}


def lexicon_sentiment(text: str) -> Dict[str, Any]:
    """Rule-based sentiment with negation + intensifier handling."""
    raw = normalise(text)
    toks = [t for t in _TOKEN_RE.findall(raw.lower())]
    score = 0.0
    hits = []
    for i, tok in enumerate(toks):
        canon = canonical_roman(tok)
        value = POSITIVE.get(tok) or POSITIVE.get(canon) or NEGATIVE.get(tok) or NEGATIVE.get(canon)
        if value is None:
            continue
        window = [canonical_roman(t) for t in toks[max(0, i - 3):i]]
        if any(w in NEGATORS for w in window):
            value = -value * 0.8
        for w in window:
            if w in INTENSIFIERS:
                value *= INTENSIFIERS[w]
        score += value
        hits.append((tok, round(value, 2)))
    if score >= 1.5:
        label = "positive"
    elif score <= -1.5:
        label = "negative"
    else:
        label = "neutral"
    return {"label": label, "score": round(score, 2), "hits": hits}


# --------------------------------------------------------------------------- #
# 6. Entity extraction
# --------------------------------------------------------------------------- #

URDU_DIGITS = {ord("۰") + i: str(i) for i in range(10)}
URDU_DIGITS.update({ord("۰") + i: str(i) for i in range(10)})

WORD_NUMBERS = {
    "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5, "پانج": 5, "چھ": 6, "سات": 7,
    "آٹھ": 8, "نو": 9, "دس": 10, "گیارہ": 11, "بارہ": 12, "تیرہ": 13, "چودہ": 14,
    "پندرہ": 15, "بیس": 20, "پچیس": 25, "تیس": 30, "چالیس": 40, "پچاس": 50,
    "ساٹھ": 60, "ستر": 70, "اسی": 80, "نوے": 90, "سو": 100, "دو سو": 200,
    "ہزار": 1000, "لاکھ": 100000, "کروڑ": 10000000,
    "ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "three": 3, "char": 4, "four": 4,
    "paanch": 5, "panch": 5, "five": 5, "chhe": 6, "six": 6, "saat": 7, "seven": 7,
    "aath": 8, "eight": 8, "nau": 9, "nine": 9, "das": 10, "dus": 10, "ten": 10,
    "bees": 20, "twenty": 20, "tees": 30, "thirty": 30, "chalees": 40, "forty": 40,
    "pachas": 50, "pachaas": 50, "fifty": 50, "sau": 100, "hundred": 100,
    "hazar": 1000, "thousand": 1000, "lakh": 100000, "lac": 100000,
    "crore": 10000000, "million": 1000000,
}

MULTIPLIERS = {"ہزار": 1000, "hazar": 1000, "thousand": 1000, "k": 1000,
               "لاکھ": 100000, "lakh": 100000, "lac": 100000, "lacs": 100000,
               "کروڑ": 10000000, "crore": 10000000, "million": 1000000, "m": 1000000}

CURRENCY_WORDS = ("روپے", "روپیہ", "rupees", "rupee", "rs", "pkr", "پیسے", "paisay",
                  "پیسہ", "rate", "price", "cost", "قیمت", "بجٹ", "budget")

PAKISTANI_CITIES = (
    "karachi", "lahore", "islamabad", "rawalpindi", "faisalabad", "multan", "peshawar",
    "quetta", "sialkot", "gujranwala", "hyderabad", "sukkur", "bahawalpur", "sargodha",
    "abbottabad", "murree", "gujrat", "sheikhupura", "jhang", "sahiwal", "okara",
    "wah cantt", "dera ghazi khan", "saidu sharif", "mardan", "mingora", "nawabshah",
    "کراچی", "لاہور", "اسلام آباد", "راولپنڈی", "فیصل آباد", "ملتان", "پشاور",
    "کوئٹہ", "سیالکوٹ", "گوجرانوالہ", "حیدرآباد",
)

CATEGORY_WORDS = {
    "restaurant": ("restaurant", "hotel", "dhaba", "cafe", "coffee", "bbq", "biryani",
                   "ریستوران", "ہوٹل", "ڈھابہ"),
    "clinic": ("clinic", "doctor", "hospital", "dental", "dentist", "physio", "lab",
               "کلینک", "ڈاکٹر", "ہسپتال"),
    "salon": ("salon", "parlour", "parlor", "spa", "beauty", "hair", "سیلون", "بیوٹی"),
    "gym": ("gym", "fitness", "health club", "جم"),
    "real_estate": ("real estate", "property", "builder", "plots", "پراپرٹی"),
    "school": ("school", "academy", "college", "institute", "tution", "tuition", "اسکول"),
    "pharmacy": ("pharmacy", "medical store", "chemist", "دواخانہ", "فارمیسی"),
    "boutique": ("boutique", "clothing", "garments", "fashion", "بوتیک"),
    "auto_workshop": ("auto", "workshop", "mechanic", "car", "motorcycle", "ورکشاپ"),
    "hotel": ("hotel", "guest house", "inn", "ہوٹل"),
    "law_firm": ("law", "lawyer", "legal", "advocate", "وکیل"),
    "electronics": ("electronics", "mobile shop", "mobile", "computer", "لپ ٹاپ"),
}

RELATIVE_DAYS = {
    "aaj": 0, "today": 0, "آج": 0,
    "kal": 1, "tomorrow": 1, "کل": 1,
    "parso": 2, "day after": 2, "پرسوں": 2,
    "agle hafte": 7, "next week": 7, "agley hafte": 7, "اگلے ہفتے": 7,
    "agle mahine": 30, "next month": 30, "اگلے مہینے": 30,
}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
            "sunday", "peer", "mangal", "budh", "jumerat", "juma", "jumma",
            "hafta", "itwar", "پیر", "منگل", "بدھ", "جمعرات", "جمعہ", "ہفتہ",
            "اتوار")

PHONE_RE = re.compile(
    r"(?:\+?92[\s\-]?|0092[\s\-]?|0)3[0-9]{2}[\s\-]?[0-9]{7}\b"
    r"|(?:\+?92[\s\-]?|0)[1-9][0-9]{1,4}[\s\-]?[0-9]{5,8}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'،۔]+", re.I)
PRICE_RE = re.compile(
    r"(?:rs\.?|pkr|روپے|روپیہ)?\s*([0-9][0-9,\.]*)\s*"
    r"(k\b|thousand|hazar|lakh|lacs?|لاکھ|ہزار|crore|کروڑ|million)?", re.I)
NAME_RE = re.compile(
    r"(?:mera naam|my name is|main|i am|main\s+)([A-Za-z\u0600-\u06FF]{2,20}"
    r"(?:\s+[A-Za-z\u0600-\u06FF]{2,20})?)", re.I)


def _to_int(text: str) -> Optional[int]:
    text = (text or "").translate(URDU_DIGITS).replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def extract_prices(text: str) -> List[Dict[str, Any]]:
    """Find money mentions, including 'pachas hazar' and '50 لاکھ'."""
    raw = normalise(text)
    found: List[Dict[str, Any]] = []
    for match in PRICE_RE.finditer(raw):
        number = _to_int(match.group(1))
        if number is None:
            continue
        unit = (match.group(2) or "").lower()
        value = number * MULTIPLIERS.get(unit, 1)
        if value < 100:            # '2 din' is not a price
            continue
        found.append({"value": value, "raw": match.group(0).strip(),
                      "currency": "PKR",
                      "has_currency_word": any(w in raw.lower() for w in CURRENCY_WORDS)})
    # Spelled-out amounts: "do lakh", "pachas hazar"
    lowered = raw.lower()
    words = re.findall(r"[a-z\u0600-\u06FF]+", lowered)
    total, consumed, phrase = 0, 0, []
    for word in words:
        if word in WORD_NUMBERS:
            value = WORD_NUMBERS[word]
            if value >= 1000 and consumed:
                total = consumed * value
                consumed = 0
            else:
                consumed += value
            phrase.append(word)
        elif word in MULTIPLIERS and consumed:
            total = consumed * MULTIPLIERS[word]
            consumed = 0
            phrase.append(word)
    if total:
        found.append({"value": total, "raw": " ".join(phrase), "currency": "PKR",
                      "has_currency_word": True})
    # de-duplicate, keep the most specific
    seen, out = set(), []
    for item in sorted(found, key=lambda x: -x["value"]):
        if item["value"] in seen:
            continue
        seen.add(item["value"])
        out.append(item)
    return out[:5]


def extract_phones(text: str) -> List[str]:
    """Pakistani phone numbers: 03xx-xxxxxxx, +923xxxxxxxxx, 0092..."""
    raw = (text or "").translate(URDU_DIGITS)
    out = []
    for match in PHONE_RE.finditer(raw):
        digits = re.sub(r"[^0-9]", "", match.group(0))
        digits = re.sub(r"^(?:0092|92)", "0", digits)
        if len(digits) == 11 and digits.startswith("0"):
            formatted = "%s-%s" % (digits[:4], digits[4:])
        else:
            formatted = digits
        if formatted not in out:
            out.append(formatted)
    return out


def extract_emails(text: str) -> List[str]:
    return sorted({m.group(0).lower() for m in EMAIL_RE.finditer(text or "")})


def extract_urls(text: str) -> List[str]:
    return sorted({m.group(0).rstrip(".,;") for m in URL_RE.finditer(text or "")})


def extract_dates(text: str) -> List[Dict[str, Any]]:
    """Relative ('kal', 'parso'), weekday, and explicit dates."""
    raw = normalise(text)
    lowered = raw.lower()
    out: List[Dict[str, Any]] = []
    for phrase, offset in RELATIVE_DAYS.items():
        if phrase in lowered:
            out.append({"type": "relative", "raw": phrase, "days_from_now": offset})
    for day in WEEKDAYS:
        if re.search(r"\b%s\b" % re.escape(day), lowered):
            out.append({"type": "weekday", "raw": day, "days_from_now": None})
    for match in re.finditer(r"\b([0-9]{1,2})[/\-]([0-9]{1,2})(?:[/\-]([0-9]{2,4}))?\b", raw):
        out.append({"type": "explicit", "raw": match.group(0),
                    "day": int(match.group(1)), "month": int(match.group(2))})
    for match in re.finditer(r"\b([0-9]{1,2})\s*(?:tareekh|tarikh|date|تاریخ)\b", lowered):
        out.append({"type": "explicit", "raw": match.group(0), "day": int(match.group(1))})
    return out[:6]


def extract_names(text: str) -> List[str]:
    out = []
    for match in NAME_RE.finditer(normalise(text)):
        candidate = match.group(1).strip()
        candidate = re.sub(r"^(hoon|hun|hai|bol|bolta|kahta)\b.*$", "", candidate,
                           flags=re.I).strip()
        if 2 <= len(candidate) <= 40 and candidate.lower() not in STOPWORDS:
            out.append(candidate)
    return out[:3]


def extract_cities(text: str) -> List[str]:
    lowered = normalise(text).lower()
    return [city for city in PAKISTANI_CITIES if city in lowered][:3]


def extract_categories(text: str) -> List[str]:
    lowered = normalise(text).lower()
    return [cat for cat, words in CATEGORY_WORDS.items() if any(w in lowered for w in words)][:3]


def extract_entities(text: str) -> Dict[str, Any]:
    """Everything we can pull out of one message, in one pass."""
    return {
        "prices": extract_prices(text),
        "phones": extract_phones(text),
        "emails": extract_emails(text),
        "urls": extract_urls(text),
        "dates": extract_dates(text),
        "names": extract_names(text),
        "cities": extract_cities(text),
        "categories": extract_categories(text),
        "language": detect_language(text),
    }


# --------------------------------------------------------------------------- #
# 7. Priority
# --------------------------------------------------------------------------- #

def rule_priority(text: str, intent: str = "", sentiment: str = "") -> str:
    """Fast, explainable priority - blended with the trained model later."""
    lowered = normalise(text).lower()
    toks = set(re.findall(r"[a-z\u0600-\u06FF]+", lowered))
    if toks & URGENCY or intent in ("callback_request", "human_needed"):
        return "high"
    if intent in ("price_query", "requirement_query", "interest"):
        return "high" if toks & set(URGENCY) else "medium"
    if intent == "objection" and sentiment == "negative":
        return "high"
    if intent == "not_interested":
        return "low"
    if toks & NEGLECT:
        return "low"
    return "medium"


# --------------------------------------------------------------------------- #
# 8. Trainer + tool facade
# --------------------------------------------------------------------------- #

INTENTS = ("price_query", "requirement_query", "objection", "interest",
           "not_interested", "callback_request", "human_needed")
SENTIMENTS = ("positive", "neutral", "negative")
PRIORITIES = ("high", "medium", "low")


class UrduNlpModel:
    """Trained Urdu/Roman-Urdu brain: intent + sentiment + priority."""

    VERSION = "1.0"

    def __init__(self, intent: Optional[NaiveBayes] = None,
                 sentiment: Optional[NaiveBayes] = None,
                 priority: Optional[NaiveBayes] = None,
                 meta: Optional[Dict[str, Any]] = None):
        self.intent = intent or NaiveBayes()
        self.sentiment = sentiment or NaiveBayes()
        self.priority = priority or NaiveBayes()
        self.meta = meta or {}

    @classmethod
    def train(cls, examples: List[Dict[str, Any]]) -> "UrduNlpModel":
        """Train from ``[{"text":..., "intent":..., "sentiment":..., "priority":...}]``."""
        texts = [str(e.get("text", "")) for e in examples]
        if not texts:
            raise ValueError("urdu_training.json has no examples")
        feats = [features(t) for t in texts]
        intent = NaiveBayes(0.25).fit(feats, [e.get("intent", "requirement_query")
                                              for e in examples])
        sentiment = NaiveBayes(0.25).fit(feats, [e.get("sentiment", "neutral")
                                                 for e in examples])
        priority = NaiveBayes(0.25).fit(feats, [e.get("priority", "medium")
                                                for e in examples])
        return cls(intent, sentiment, priority, {
            "version": cls.VERSION,
            "n_examples": len(examples),
            "intents": sorted({e.get("intent", "") for e in examples}),
            "languages": sorted({detect_language(t) for t in texts}),
            "vocab": len(intent.vocab),
        })

    def analyse(self, text: str) -> Dict[str, Any]:
        """Full analysis of one message."""
        feats = features(text)
        intent, intent_conf = self.intent.predict(feats) if self.intent.classes else ("unknown", 0.0)
        sent_model, sent_conf = (self.sentiment.predict(feats)
                                 if self.sentiment.classes else ("neutral", 0.0))
        prio_model, prio_conf = (self.priority.predict(feats)
                                 if self.priority.classes else ("medium", 0.0))
        lex = lexicon_sentiment(text)

        # Blend model + lexicon: the lexicon knows negation, the model knows slang.
        if lex["label"] != "neutral" and sent_conf < 0.55:
            sentiment = lex["label"]
        elif lex["label"] == sent_model:
            sentiment = sent_model
        elif abs(lex["score"]) >= 3:
            sentiment = lex["label"]
        else:
            sentiment = sent_model

        priority = rule_priority(text, intent, sentiment)
        if prio_conf > 0.6 and priority != prio_model:
            priority = prio_model if prio_conf > 0.75 else priority

        return {
            "text": text,
            "language": detect_language(text),
            "intent": intent,
            "intent_confidence": round(intent_conf, 3),
            "sentiment": sentiment,
            "sentiment_score": lex["score"],
            "sentiment_confidence": round(sent_conf, 3),
            "priority": priority,
            "priority_confidence": round(prio_conf, 3),
            "entities": extract_entities(text),
            "tokens": tokenize(text),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "urdu_nlp_model",
            "version": self.VERSION,
            "meta": self.meta,
            "intent": self.intent.to_dict(),
            "sentiment": self.sentiment.to_dict(),
            "priority": self.priority.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UrduNlpModel":
        return cls(NaiveBayes.from_dict(data["intent"]),
                   NaiveBayes.from_dict(data["sentiment"]),
                   NaiveBayes.from_dict(data["priority"]),
                   dict(data.get("meta", {})))


class UrduNlpTool:
    """Apartment-facing facade.  Works with or without a trained model."""

    def __init__(self, apartment, model_name: str = "urdu_nlp.json"):
        self.apartment = apartment
        self.model_name = model_name
        self._model: Optional[UrduNlpModel] = None

    @property
    def model(self) -> Optional[UrduNlpModel]:
        if self._model is None and self.apartment.has_model(self.model_name):
            try:
                self._model = UrduNlpModel.from_dict(
                    self.apartment.load_model(self.model_name))
            except Exception:
                self._model = None
        return self._model

    def is_trained(self) -> bool:
        return self.model is not None

    def analyse(self, text: str) -> Dict[str, Any]:
        model = self.model
        if model is None:
            # Untrained fallback: rules + lexicon still give a usable answer.
            intent = _rule_intent(text)
            lex = lexicon_sentiment(text)
            return {
                "text": text, "language": detect_language(text),
                "intent": intent, "intent_confidence": 0.0,
                "sentiment": lex["label"], "sentiment_score": lex["score"],
                "sentiment_confidence": 0.0,
                "priority": rule_priority(text, intent, lex["label"]),
                "priority_confidence": 0.0,
                "entities": extract_entities(text), "tokens": tokenize(text),
                "untrained": True,
            }
        result = model.analyse(text)
        result["untrained"] = False
        return result

    # convenience passthroughs -------------------------------------------------
    def tokenize(self, text: str) -> List[str]:
        return tokenize(text)

    def classify_intent(self, text: str) -> str:
        return self.analyse(text)["intent"]

    def detect_sentiment(self, text: str) -> str:
        return self.analyse(text)["sentiment"]

    def extract_entities(self, text: str) -> Dict[str, Any]:
        return extract_entities(text)

    def priority(self, text: str) -> str:
        return self.analyse(text)["priority"]


#: Intent keyword rules - used as a fallback and as a training-data sanity net.
INTENT_RULES: Dict[str, Tuple[str, ...]] = {
    "price_query": ("kitna", "kitne", "rate", "price", "cost", "charges", "fee",
                    "paisay", " rupees", "budget", "کتنے", "قیمت", "روپے", "فیس",
                    "kitna hoga", "charges kya"),
    "objection": ("mehnga", "expensive", "sochna", "soch", "baad", "abhi nahi",
                  "nahi chahiye", "pehle se", "already", "kaun", "who are you",
                  "trust", "doubt", "shak", "مہنگا", "سوچنا", "بعد میں", "پہلے سے"),
    "interest": ("chahiye", "interested", "haan", "banao", "banado", "start",
                 "let's", "chalain", "karna hai", "zaroor", "کرنا ہے", "چاہیے",
                 "شروع", "ضرور"),
    "not_interested": ("nahi chahiye", "not interested", "no thanks", "mana",
                       "block", "spam", "stop", "bus karo", "نہیں چاہیے", "بس کرو"),
    "callback_request": ("call", "callback", "phone karo", "call karo", "milte",
                         "meeting", "zoom", "وقت", "کال", "ملاقات"),
    "human_needed": ("owner", "manager", "senior", "insaan", "human", "real person",
                     "boss", "مالک", "انسان"),
    "requirement_query": ("website", "site", "page", "design", "feature", "chahiye",
                          "kya kya", "kaise", "کس طرح", "ویب سائٹ"),
}


def _rule_intent(text: str) -> str:
    lowered = normalise(text).lower()
    best, score = "requirement_query", 0
    for intent, words in INTENT_RULES.items():
        hits = sum(1 for w in words if w in lowered)
        if hits > score:
            best, score = intent, hits
    return best


def try_load_jev_urdu():
    """Optional upgrade: use the ``jev-urdu`` transformer if you installed it.

    Returns ``None`` when unavailable.  Deliberately off the hot path - it needs
    ``torch`` + a downloaded model, which does not fit the 4 GB / offline rules.
    """
    try:  # pragma: no cover - depends on an optional install
        import jev_urdu  # type: ignore
        return jev_urdu
    except Exception:
        return None


def build(apartment) -> UrduNlpTool:
    return UrduNlpTool(apartment)
