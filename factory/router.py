"""Intent router - decides which apartment should answer a message.

Trained by ``train_all.py`` (``IntentRouterTrainer``) into
``apartments/apt_knowledge/models/router.json``: a multinomial Naive Bayes
classifier written from scratch over word + Roman-Urdu-marker features.

If the model is missing or confidence is low, a deterministic keyword-and-phone
fallback decides, and the router *always* returns a guess - the orchestrator
would rather hand the message somewhere sensible than drop it.

Zero dependencies.  JSON models only, never pickle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .ml import tokenize

MODEL_NAME = "router.json"


class IntentRouter:
    """Loads the trained router and routes text to an apartment key."""

    FALLBACK = "knowledge"

    def __init__(self, knowledge_apartment: Any = None, threshold: float = 0.18):
        self.apartment = knowledge_apartment
        self.threshold = threshold
        self.model: Dict[str, Any] = {}
        self.loaded = False

    # ------------------------------------------------------------------- load
    def load(self) -> bool:
        if self.apartment is None:
            return False
        try:
            model = self.apartment.load_model(MODEL_NAME, default=None)
        except Exception:
            model = None
        if isinstance(model, dict) and model.get("classes"):
            self.model = model
            self.loaded = True
        return self.loaded

    # ------------------------------------------------------------------ route
    #: Shell-looking input must go to the manager, never to the chat agents.
    COMMAND_MARKERS = ("powershell mein", "powershell me", "pwsh mein", "cmd mein",
                       "cmd me", "terminal mein", "bash mein", "shell mein",
                       "command chalao", "run karo", "chalao")

    NOT_A_COMMAND = ("kya", "kaise", "kyun", "kyu", "kab", "kahan", "kon", "kaun",
                     "hai", "hain", "ho", "hun", "karo", "karein", "batao", "bataiye",
                     "chahiye", "please", "policy", "rate", "price", "kitna", "kitne",
                     "mujhe", "humein", "hum", "aap", "mera", "meri", "the", "is",
                     "are", "what", "how", "why", "when", "show", "tell")

    def _looks_like_command(self, text: str) -> bool:
        """True for 'dir', 'ipconfig', 'Get-Process', 'git status', 'ls -la'..."""
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        if any(marker in lowered for marker in self.COMMAND_MARKERS):
            return True
        # 'free mockup policy kya hai' starts with a command name but is a
        # question - sentence words mean it is chat, not a shell line.
        words = set(lowered.replace("?", " ").replace(",", " ").split())
        if words & set(self.NOT_A_COMMAND):
            return False
        import re as _re

        head = _re.split(r"[\s;|&]+", lowered)[0].strip("`\"'")
        if head in COMMAND_VERBS:
            return True
        return head in _allowlisted_exes()

    #: Words that prove the input is a sentence, not a shell command.
    NOT_A_COMMAND = ("kya", "kaise", "kyun", "kyu", "kab", "kahan", "kon", "kaun",
                     "hai", "hain", "ho", "hun", "karo", "karein", "batao", "bataiye",
                     "chahiye", "please", "policy", "rate", "price", "kitna", "kitne",
                     "mujhe", "humein", "hum", "aap", "mera", "meri", "the", "is",
                     "are", "what", "how", "why", "when", "show", "tell")

    def route(self, text: str) -> Tuple[str, float, str]:
        """Return ``(apartment_key, confidence, why)``."""
        if self._looks_like_command(text):
            return "manager", 0.95, "shell-command"
        if not self.loaded and not self.load():
            return self._fallback(text)
        scores = self._scores(text)
        if not scores:
            return self._fallback(text)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, best_score = ordered[0]
        margin = best_score - (ordered[1][1] if len(ordered) > 1 else -9.0)
        if best_score < self.threshold and margin < 0.05:
            fallback = self._fallback(text)
            if fallback[1] > best_score:
                return fallback
        return best, round(float(best_score), 4), "naive-bayes"

    def _scores(self, text: str) -> Dict[str, float]:
        model = self.model
        priors = model.get("priors", {})
        log_likelihoods = model.get("log_likelihoods", {})
        smoothing = float(model.get("smoothing", 0.2))
        tokens = tokenize(text)
        if not tokens:
            return {}
        vocabulary = set(model.get("vocabulary", []))
        scores: Dict[str, float] = {}
        oov = math_log(smoothing / max(1, len(vocabulary) or 1))
        for label, prior in priors.items():
            score = math_log(float(prior))
            table = log_likelihoods.get(label, {})
            for token in tokens:
                score += float(table.get(token, oov))
            scores[label] = score
        return _softmax(scores)

    # --------------------------------------------------------------- fallback
    KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("manager", ("status", "kaun kaam kar raha", "agent", "workers", "pause",
                     "restart", "band karo", "task", "queue", "ram", "screenshot",
                     "machine", "system", "powershell", "pwsh", "cmd", "terminal",
                     "bash", "shell mein", "command chalao", "chalao",
                     # common shell commands themselves
                     "dir", "ls ", "ipconfig", "systeminfo", "tasklist", "whoami",
                     "get-process", "get-childitem", "df -h", "uname", "top",
                     "free -m", "ping ", "netstat", "echo ")),
        ("security", ("security", "audit chain", "secret", "hack", "scan karo",
                      "injection", "vulnerab")),
        ("self_trainer", ("learn", "seekho", "retrain", "improve", "top patterns",
                          "kaun sa message chal raha")),
        ("hunter", ("dhoondo", "dhondo", "find", "scrape", "leads lao", "leads chahiye",
                    "naye client", "search kar", "karachi", "lahore", "islamabad")),
        ("platform_whatsapp", ("whatsapp", "wa par", "green app")),
        ("platform_linkedin", ("linkedin", "connection request", "inmail")),
        ("platform_facebook", ("facebook", "fb group", "messenger")),
        ("platform_instagram", ("instagram", "insta", "dm kar", "reel")),
        ("platform_github", ("github", "repo", "pages par", "commit")),
        ("builder", ("site banao", "demo", "mockup", "preview", "website bana")),
        ("auditor", ("audit", "site check", "speed test", "ssl", "mobile score")),
        ("enricher", ("email nika", "enrich", "contact details", "phone number")),
        ("outreach", ("message bhejo", "outreach", "pehla message", "contact karo",
                      "cold email")),
        ("inbox", ("reply", "inbox", "jawab aya", "responses", "unread")),
        ("requirements", ("brief", "requirements", "sawal poocho", "discovery")),
        ("convincer", ("mehnga", "objection", "sochna hai", "trust", "time nahi",
                       "competitor", "family se pooch")),
        ("closer", ("quote", "price bhejo", "invoice", "package", "close", "deal",
                    "advance", "contract")),
        ("followup", ("followup", "follow up", "dobara message", "yaad dila")),
        ("urdu_nlp", ("urdu", "roman urdu", "sentiment", "translate", "matlab kya")),
        ("knowledge", ("pricing", "rate card", "faq", "portfolio", "policy",
                       "kitna charge", "guarantee")),
    )

    def _fallback(self, text: str) -> Tuple[str, float, str]:
        lowered = (text or "").lower()
        best, hits = self.FALLBACK, 0
        for label, words in self.KEYWORDS:
            count = sum(1 for word in words if word in lowered)
            if count > hits:
                best, hits = label, count
        confidence = 0.25 + 0.15 * hits if hits else 0.1
        return best, round(confidence, 4), "keyword-fallback"

    # ------------------------------------------------------------- inspection
    def labels(self) -> List[str]:
        return sorted((self.model.get("classes") or
                       [label for label, _ in self.KEYWORDS]))

    def explain(self, text: str, top: int = 3) -> List[Dict[str, Any]]:
        scores = self._scores(text)
        if not scores:
            return []
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top]
        return [{"apartment": label, "confidence": round(float(score), 3)}
                for label, score in ordered]


#: First words that mean "this is a shell command", regardless of language.
COMMAND_VERBS = frozenset("""
dir ls cd cat type echo copy move del rm mkdir rmdir ren rename cls clear
ipconfig ipconfig /all systeminfo tasklist taskkill whoami hostname ver wmic
netstat ping tracert nslookup net schtasks sfc chkdsk dism
get-process get-childitem get-date get-service get-content get-item
set-location new-item remove-item select-object where-object measure-object
ps top df du free uname uptime kill killall which where find findstr grep
head tail wc sort uniq chmod chown curl wget tar zip unzip
git python pip pytest node npm
""".split())


def _allowlisted_exes() -> frozenset:
    """Executable names the terminal tool permits - same source of truth."""
    try:
        from .apartments.tools.terminal import ALLOWLIST
        return frozenset(ALLOWLIST.keys())
    except Exception:
        return frozenset()


def math_log(value: float) -> float:
    import math

    return math.log(max(1e-12, float(value)))


def _softmax(scores: Dict[str, float]) -> Dict[str, float]:
    import math

    if not scores:
        return {}
    highest = max(scores.values())
    exponentials = {key: math.exp(value - highest) for key, value in scores.items()}
    total = sum(exponentials.values()) or 1.0
    return {key: value / total for key, value in exponentials.items()}
