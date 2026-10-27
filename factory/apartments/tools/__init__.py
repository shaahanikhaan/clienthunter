"""Shared tools available to apartments.

Every tool is constructed through a ``build(apartment)`` factory and enforces
its own permission check at call time, so a tool handed to the wrong apartment
still cannot do damage.

All tools degrade gracefully: when the network (or Chrome, or SMTP) is absent
they log the attempt and return a sentinel instead of crashing the factory.
That is what makes ``run_demo.py`` work on an offline machine.

Optional third-party accelerators are detected at import time and used only
when present - see POWER_GUIDE.md for the full list and the honest notes about
which of the suggested libraries actually exist.
"""

from typing import Any

#: Import order matters only for readability; each module is standalone.
_TOOLS = (
    "file",        # sandboxed filesystem (stdlib)
    "web",         # urllib fetch + DDG search, robots.txt + rate limits
    "scrape",      # Google Maps + directories (CDP) with CSV fallback
    "maps_api",    # Google Maps internal endpoints - keyless, no Chrome needed
    "verify",      # email/phone deliverability: raw DNS MX, PK number rules
    "enrich",      # 6-layer email/phone/social extraction
    "audit",       # website audit: SSL, speed, mobile, SEO, links
    "email",       # IMAP/SMTP (waggle-mail when installed)
    "browser",     # CDP over real Chrome (ricibrowser optional)
    "whatsapp",    # waeys (pure-Python Baileys) or WhatsApp Web over CDP
    "github",      # git + git-credential-oauth, no PAT
    "mouse",       # pyautogui / AutoControl / ctypes user32
    "screen",      # screenshots + OCR/template matching
    "nlp_urdu",    # Urdu + Roman Urdu NLP, self-trained
    "llm_local",   # n-gram generator (optional GGUF tier-2)
    "terminal",    # CMD / PowerShell / bash, allowlist + denylist, argv-only
)

__all__ = list(_TOOLS) + ["build_all", "describe_backends"]


def build_all(apartment) -> Any:
    """Eagerly build every tool for an apartment (used by tests)."""
    import importlib

    return {name: importlib.import_module(__name__ + "." + name).build(apartment)
            for name in _TOOLS}


def describe_backends(apartment) -> Any:
    """Report which optional libraries are actually available on this machine."""
    import importlib
    import sys

    def _has(module: str) -> bool:
        try:
            importlib.import_module(module)
            return True
        except Exception:
            return False

    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "ricibrowser": _has("ricibrowser"),
        "waggle_mail": _has("waggle_mail") or _has("waggle"),
        "waeys": _has("WAeys") or _has("waeys"),
        "pyautogui": _has("pyautogui"),
        "pynput": _has("pynput"),
        "pillow": _has("PIL"),
        "auto_control": _has("je_auto_control"),
        "coldreach": _has("coldreach"),
        "jev_urdu": _has("jev_urdu"),
        "llama_cpp": _has("llama_cpp"),
    }
