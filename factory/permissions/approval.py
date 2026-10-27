"""Human-in-the-loop approval.

The factory asks before it does anything visible to another human, or anything
that changes shared state.  Asking happens through:

1.  a CLI prompt with a real countdown (works everywhere, no dependencies),
2.  an optional Windows popup (``MessageBoxTimeout`` via ``ctypes``) that closes
    itself when the timer expires,
3.  a headless policy fallback when there is no terminal at all (cron, CI,
    background service) - medium defaults to allow, high defaults to deny.

A timeout is never treated as a "yes" for high-risk actions.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, Optional


class ApprovalResult:
    __slots__ = ("approved", "timed_out", "remember", "note")

    def __init__(self, approved: bool, timed_out: bool = False, remember: bool = False,
                 note: str = ""):
        self.approved = bool(approved)
        self.timed_out = bool(timed_out)
        self.remember = bool(remember)
        self.note = note

    def __bool__(self) -> bool:
        return self.approved

    def __repr__(self) -> str:  # pragma: no cover
        return "<ApprovalResult approved=%s timed_out=%s>" % (self.approved, self.timed_out)


class Approver:
    """Asks the human.  One instance, reused for the whole factory session."""

    #: Answers that mean yes / always-yes / no.
    YES = ("y", "yes", "haan", "han", "ha", "allow", "ok")
    ALWAYS = ("a", "always", "y all", "yes all")
    NO = ("n", "no", "nahi", "deny", "block")

    def __init__(self, notify: bool = True, bell: bool = True):
        self.notify = notify
        self.bell = bell
        self.auto_yes = False          # set by --yes CLI flag
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ main
    def request(self, agent: str, action: str, risk: str, details: Dict[str, Any],
                timeout: int = 60) -> ApprovalResult:
        """Ask the user.  Returns an :class:`ApprovalResult`, never raises."""
        if self.auto_yes:
            return ApprovalResult(True, note="--yes flag")
        summary = self.summarise(agent, action, risk, details)
        self._notify(summary, timeout)
        answer = self._prompt(summary, timeout)
        return answer

    def summarise(self, agent: str, action: str, risk: str, details: Dict[str, Any]) -> str:
        bits = ["[%s] %s wants to: %s  (risk: %s)" % (risk.upper(), agent, action, risk)]
        for key in ("to", "phone", "subject", "url", "domain", "repo", "path", "lead",
                    "platform", "message"):
            if key in details and details[key]:
                value = str(details[key])
                if len(value) > 220:
                    value = value[:217] + "..."
                bits.append("    %-8s %s" % (key + ":", value))
        return "\n".join(bits)

    # -------------------------------------------------------------- transports
    def _notify(self, summary: str, timeout: int) -> None:
        """Desktop popup where available (Windows), console bell otherwise."""
        print("\n" + "=" * 74, flush=True)
        print(summary, flush=True)
        if self.bell:
            sys.stdout.write("\a")   # terminal bell - works on Windows too
            sys.stdout.flush()
        if self.notify:
            self._windows_popup(summary, timeout)

    @staticmethod
    def _windows_popup(text: str, timeout: int) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            title = "ClientHunter Factory - approval needed"
            body = text + "\n\nSee the terminal to answer."
            # MessageBoxTimeoutW: 0 = no icon, 0x40000 = topmost
            ctypes.windll.user32.MessageBoxTimeoutW(0, body, title, 0 | 0x40000, 0,
                                                    int(timeout * 1000))
        except Exception:
            pass

    def _prompt(self, summary: str, timeout: int) -> ApprovalResult:
        if not (sys.stdin and sys.stdin.isatty()):
            return ApprovalResult(False, timed_out=True, note="no tty")

        box: Dict[str, Optional[str]] = {"answer": None}
        stop = threading.Event()

        def reader():
            try:
                box["answer"] = sys.stdin.readline()
            except Exception:
                box["answer"] = None
            finally:
                stop.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and not stop.is_set():
                left = int(deadline - time.time())
                sys.stdout.write("\r  Allow? (y)es / (n)o / (a)lways  [auto-deny in %2ds] "
                                 % max(left, 0))
                sys.stdout.flush()
                stop.wait(0.25)
        finally:
            sys.stdout.write("\r" + " " * 70 + "\r")
            sys.stdout.flush()

        if not stop.is_set():
            return ApprovalResult(False, timed_out=True, note="timeout %ds" % timeout)
        raw = (box["answer"] or "").strip().lower()
        if raw in self.ALWAYS:
            return ApprovalResult(True, remember=True, note="always")
        if raw in self.YES:
            return ApprovalResult(True, note="yes")
        return ApprovalResult(False, note="no")


class AutoApprover(Approver):
    """Approves everything - for tests and unattended demos only.

    Deliberately loud: it prints every decision so it can never silently
    rubber-stamp a real send.
    """

    def request(self, agent: str, action: str, risk: str, details: Dict[str, Any],
                timeout: int = 60) -> ApprovalResult:
        print("[AUTO-APPROVE] %s / %s (%s)" % (agent, action, risk))
        return ApprovalResult(True, note="auto approver")
