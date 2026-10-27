"""GitHub tool - OAuth device flow through ``git``'s credential helper.

No Personal Access Token.  No SSH key.  No cloud service.

`git-credential-oauth <https://github.com/hickford/git-credential-oauth>`_ is a
Git credential helper that opens your browser for an OAuth login the first time
you push, then caches the token in the OS key store.  Every later push is
non-interactive.  Git itself is the only "SDK" - this tool shells out to it.

Behaviour:
  * ``ensure_credential_helper()`` - installs the helper into your global git
    config if the binary exists; otherwise prints the one-line install command
    and carries on (nothing is downloaded automatically).
  * Every mutating operation is repo-allowlisted and permission-gated;
    ``git_push`` is HIGH risk, so a human approves it (or it is denied when
    headless).
  * ``clone`` is refused for any host outside the allowlist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_HELPER = "oauth"
INSTALL_HINTS = {
    "win32": "winget install hickford.git-credential-oauth   (then restart the terminal)",
    "darwin": "brew install git-credential-oauth",
    "linux": "sudo apt install git-credential-oauth   # or download the release binary",
}


class GitHubTool:
    """Safe, allowlisted git automation."""

    def __init__(self, apartment):
        self.apartment = apartment
        self.helper = DEFAULT_HELPER
        self.timeout = 120

    # ---------------------------------------------------------------- plumbing
    def _run(self, args: Sequence[str], cwd: Optional[Path] = None,
             check: bool = False) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(
                list(args), cwd=str(cwd) if cwd else None,
                capture_output=True, text=True, timeout=self.timeout,
                encoding="utf-8", errors="replace")
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except FileNotFoundError:
            return 127, "", "git not found - install Git for Windows / git"
        except subprocess.TimeoutExpired:
            return 124, "", "git timed out"

    def available(self) -> bool:
        return shutil.which("git") is not None

    # ------------------------------------------------------------------ helper
    def ensure_credential_helper(self) -> Dict[str, Any]:
        """Wire up ``git-credential-oauth`` if it is installed."""
        if not self.available():
            return {"ok": False, "reason": "git not installed"}
        code, out, err = self._run(["git", "config", "--global", "credential.helper"])
        helpers = [line for line in out.splitlines() if line.strip()]
        if any(self.helper in h for h in helpers):
            return {"ok": True, "status": "already configured", "helpers": helpers}
        if not shutil.which("git-credential-" + self.helper):
            return {
                "ok": False,
                "reason": "git-credential-oauth binary not found",
                "install": INSTALL_HINTS.get(_platform_key(), INSTALL_HINTS["linux"]),
                "note": "Without it, git will fall back to its normal credential flow.",
            }
        code, out, err = self._run(["git", "config", "--global", "credential.helper",
                                    self.helper])
        return {"ok": code == 0, "status": "installed" if code == 0 else err,
                "helpers": [self.helper]}

    def ensure_identity(self, name: Optional[str] = None, email: Optional[str] = None) -> bool:
        agency = self.apartment.agency().get("agency", {}) or {}
        name = name or agency.get("owner_name") or "ClientHunter Factory"
        email = email or agency.get("email") or "factory@localhost"
        self._run(["git", "config", "--global", "user.name", name])
        self._run(["git", "config", "--global", "user.email", email])
        return True

    # -------------------------------------------------------------- repository
    def _approve_repo(self, repo: str, action: str) -> Optional[str]:
        """Return a denial reason, or ``None`` when the gate allows it."""
        decision = self.apartment.gate.check(self.apartment.NAME, action, {"repo": repo})
        return None if decision.allowed else decision.reason

    def repo_slug(self, path: Path) -> str:
        code, out, _ = self._run(["git", "-C", str(path), "config", "--get", "remote.origin.url"])
        if code != 0 or not out:
            return ""
        url = out.strip()
        match = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/.]+)", url)
        return match.group("slug") if match else url

    def status(self, path: Path) -> Dict[str, Any]:
        code, out, err = self._run(["git", "-C", str(path), "status", "--porcelain", "-b"])
        return {"ok": code == 0, "branch": self.current_branch(path),
                "dirty": bool([l for l in out.splitlines() if not l.startswith("##")]),
                "porcelain": out, "error": err}

    def current_branch(self, path: Path) -> str:
        code, out, _ = self._run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
        return out.strip() if code == 0 else ""

    def diff(self, path: Path, stat: bool = True) -> str:
        args = ["git", "-C", str(path), "diff", "--stat"] if stat else \
               ["git", "-C", str(path), "diff"]
        code, out, err = self._run(args)
        return out if code == 0 else err

    def create_branch(self, path: Path, name: str) -> bool:
        denied = self._approve_repo(self.repo_slug(path), "create_branch")
        if denied:
            self.apartment.log("branch_denied", reason=denied)
            return False
        code, _, err = self._run(["git", "-C", str(path), "checkout", "-b", name])
        if code != 0:
            self.apartment.log("branch_failed", error=err)
        return code == 0

    def commit(self, path: Path, message: str, add_all: bool = True) -> Dict[str, Any]:
        denied = self._approve_repo(self.repo_slug(path), "git_commit")
        if denied:
            return {"ok": False, "reason": denied}
        if add_all:
            self._run(["git", "-C", str(path), "add", "-A"])
        code, out, err = self._run(["git", "-C", str(path), "commit", "-m", message])
        if code != 0:
            return {"ok": False, "reason": err or "nothing to commit"}
        self.apartment.log("git_commit", repo=self.repo_slug(path), message=message[:80])
        return {"ok": True, "output": out, "repo": self.repo_slug(path)}

    def push(self, path: Path, branch: Optional[str] = None,
             set_upstream: bool = True) -> Dict[str, Any]:
        """Push - HIGH risk: allowlist + human approval required."""
        repo = self.repo_slug(path)
        denied = self._approve_repo(repo, "git_push")
        if denied:
            self.apartment.log("push_denied", repo=repo, reason=denied)
            return {"ok": False, "reason": denied, "risk": "high"}
        branch = branch or self.current_branch(path)
        args = ["git", "-C", str(path), "push"]
        if set_upstream:
            args += ["-u", "origin", branch]
        self.apartment.gate.enforce(self.apartment.NAME, "git_push",
                                    {"repo": repo, "branch": branch})
        code, out, err = self._run(args)
        combined = (out + "\n" + err).strip()
        if code != 0:
            self.apartment.log("push_failed", repo=repo, error=combined[:300])
            return {"ok": False, "reason": combined[:400]}
        self.apartment.log("git_push", repo=repo, branch=branch)
        return {"ok": True, "repo": repo, "branch": branch, "output": combined[:400]}

    def clone(self, url: str, dest: Path) -> Dict[str, Any]:
        """Clone - only from github.com, and only with the human's approval."""
        if "github.com" not in url:
            return {"ok": False, "reason": "only github.com remotes are allowed"}
        decision = self.apartment.gate.check(self.apartment.NAME, "git_push",
                                             {"url": url, "action": "clone"})
        if not decision.allowed:
            return {"ok": False, "reason": decision.reason}
        dest = Path(dest)
        if dest.exists() and any(dest.iterdir()):
            return {"ok": False, "reason": "destination not empty: %s" % dest}
        dest.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = self._run(["git", "clone", url, str(dest)])
        return {"ok": code == 0, "output": (out or err)[:400]}

    def init(self, path: Path) -> bool:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        code, _, _ = self._run(["git", "-C", str(path), "init"])
        return code == 0

    def log(self, path: Path, limit: int = 10) -> List[str]:
        code, out, _ = self._run(["git", "-C", str(path), "log", "--oneline",
                                  "-n", str(limit)])
        return out.splitlines() if code == 0 else []

    def info(self) -> Dict[str, Any]:
        helper = self.ensure_credential_helper()
        return {"git": self.available(), "helper": helper,
                "install_hint": INSTALL_HINTS.get(_platform_key())}


def _platform_key() -> str:
    import sys
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def build(apartment) -> GitHubTool:
    return GitHubTool(apartment)
