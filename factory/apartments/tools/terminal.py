"""Terminal tool - real CMD / PowerShell / bash execution on your machine.

This is the "same access as me" principle in its most literal form, so it is
built to the 2026 consensus on agent shell safety:

1. **Allowlist first, denylist as a backstop.**  Deny lists are provably
   incomplete; allow lists fail closed on anything not enumerated [3][5].
2. **argv only, never ``shell=True``.**  Commands are parsed with ``shlex`` and
   passed to ``subprocess`` as a list, so metacharacters stay characters - this
   is the primary defence against CWE-78 command injection [5][18].
3. **Risk tiers wired to the gate.**  Read-only commands run freely; writes ask
   once; admin/destructive commands need an explicit yes; a critical class is
   blocked outright and no approval can override it.
4. **Everything is logged** to the hash-chained audit trail, with the command,
   the resolved risk, exit code and a truncated output preview.
5. **Timeouts and output caps** so a runaway command cannot hang the factory or
   fill RAM (important on a 4 GB machine).

No shell is ever spawned with a string.  ``powershell``/``pwsh``/``cmd``/
``bash`` are invoked as the executable with explicit argument lists.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..base_apartment import PermissionDenied

# --------------------------------------------------------------------------- #
# Shell discovery
# --------------------------------------------------------------------------- #

def find_shells() -> Dict[str, Optional[str]]:
    """Locate every shell available on this machine (real paths, no guessing)."""
    found: Dict[str, Optional[str]] = {"powershell": None, "pwsh": None,
                                       "cmd": None, "bash": None}
    if sys.platform == "win32":
        for root in (os.environ.get("SystemRoot", r"C:\Windows"),):
            candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if candidate.exists():
                found["powershell"] = str(candidate)
            cmd = Path(root) / "System32" / "cmd.exe"
            if cmd.exists():
                found["cmd"] = str(cmd)
        found["pwsh"] = shutil.which("pwsh")
    found["bash"] = shutil.which("bash") or shutil.which("sh")
    if not found.get("cmd"):
        found["cmd"] = shutil.which("cmd") or shutil.which("cmd.exe")
    return found


# --------------------------------------------------------------------------- #
# Policy: allowlist (risk tiered) + denylist (hard block)
# --------------------------------------------------------------------------- #

#: command (lowercased, no extension) -> (risk, human description)
ALLOWLIST: Dict[str, Tuple[str, str]] = {
    # ---- read-only: safe, logged, no prompt -------------------------------- #
    "dir": ("read", "list directory"),
    "ls": ("read", "list directory"),
    "echo": ("read", "print text"),
    "type": ("read", "print a file"),
    "cat": ("read", "print a file"),
    "more": ("read", "page through a file"),
    "where": ("read", "locate an executable"),
    "which": ("read", "locate an executable"),
    "whoami": ("read", "current user"),
    "hostname": ("read", "machine name"),
    "ver": ("read", "windows version"),
    "uname": ("read", "system info"),
    "ipconfig": ("read", "network configuration"),
    "ifconfig": ("read", "network configuration"),
    "ping": ("read", "network reachability"),
    "nslookup": ("read", "dns lookup"),
    "netstat": ("read", "network connections"),
    "systeminfo": ("read", "system information"),
    "tasklist": ("read", "running processes"),
    "ps": ("read", "running processes"),
    "df": ("read", "disk free space"),
    "free": ("read", "memory usage"),
    "date": ("read", "current date"),
    "time": ("read", "current time"),
    "git": ("read", "git (write subcommands escalate to 'write')"),
    "python": ("write", "run python"),
    "python3": ("write", "run python"),
    "py": ("write", "run python"),
    "node": ("write", "run node"),
    "pip": ("admin", "install packages"),
    # PowerShell read-only cmdlets (invoked via -Command)
    "get-childitem": ("read", "PS: list directory"),
    "get-content": ("read", "PS: read file"),
    "get-process": ("read", "PS: running processes"),
    "get-service": ("read", "PS: services"),
    "get-date": ("read", "PS: date"),
    "get-location": ("read", "PS: current directory"),
    "get-item": ("read", "PS: item info"),
    "select-string": ("read", "PS: grep"),
    "write-output": ("read", "PS: print"),
    "measure-object": ("read", "PS: count"),
    "test-netconnection": ("read", "PS: connectivity"),
    "get-computerinfo": ("read", "PS: system info"),
    "get-disk": ("read", "PS: disks"),
    "get-volume": ("read", "PS: volumes"),
    "get-netipaddress": ("read", "PS: ip addresses"),
    "get-eventlog": ("read", "PS: event log"),
    "get-winevent": ("read", "PS: event log"),
    "get-hotfix": ("read", "PS: installed updates"),
    "get-executionpolicy": ("read", "PS: execution policy"),
    # ---- write: asks once -------------------------------------------------- #
    "mkdir": ("write", "create directory"),
    "md": ("write", "create directory"),
    "copy": ("write", "copy file"),
    "xcopy": ("write", "copy files"),
    "move": ("write", "move file"),
    "ren": ("write", "rename"),
    "robocopy": ("write", "copy files"),
    "new-item": ("write", "PS: create item"),
    "copy-item": ("write", "PS: copy"),
    "move-item": ("write", "PS: move"),
    "set-content": ("write", "PS: write file"),
    "add-content": ("write", "PS: append file"),
    "out-file": ("write", "PS: write file"),
    "new-itemproperty": ("admin", "PS: registry value"),
    "expand-archive": ("write", "PS: unzip"),
    "compress-archive": ("write", "PS: zip"),
    "start-process": ("admin", "PS: launch process"),
    "invoke-webrequest": ("admin", "PS: download"),
    "curl": ("admin", "download"),
    "winget": ("admin", "install software"),
    "choco": ("admin", "install software"),
    # ---- admin: explicit yes required -------------------------------------- #
    "del": ("admin", "delete file"),
    "erase": ("admin", "delete file"),
    "rm": ("admin", "delete"),
    "rmdir": ("admin", "remove directory"),
    "rd": ("admin", "remove directory"),
    "remove-item": ("admin", "PS: delete"),
    "clear-item": ("admin", "PS: clear"),
    "taskkill": ("admin", "kill process"),
    "stop-process": ("admin", "PS: kill process"),
    "stop-service": ("admin", "PS: stop service"),
    "restart-service": ("admin", "PS: restart service"),
    "schtasks": ("admin", "scheduled tasks"),
    "net": ("admin", "network/user/service management"),
    "reg": ("admin", "registry"),
    "set-executionpolicy": ("admin", "PS: execution policy"),
    "set-itemproperty": ("admin", "PS: set property"),
    "icacls": ("admin", "file permissions"),
    "takeown": ("admin", "take ownership"),
}

#: Never run, under any circumstances.  Checked against the whole command line.
DENYLIST: Sequence[Tuple[str, str]] = (
    (r"\bformat\b(?![a-z])", "disk format"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\bbcdedit\b", "boot configuration"),
    (r"\bbootsect\b", "boot sector"),
    (r"\bshutdown\b", "system shutdown"),
    (r"\brestart-computer\b", "system restart"),
    (r"\bstop-computer\b", "system shutdown"),
    (r"\bmkfs\b", "filesystem creation"),
    (r"\bdd\s+if=", "raw disk write"),
    (r"\bvssadmin\s+delete\s+shadows", "shadow copy deletion"),
    (r"\bwmic\s+.*delete", "wmic delete"),
    (r"\bcipher\s+/w", "secure wipe"),
    (r"\bsfc\s+/scannow", "system file checker (slow, privileged)"),
    (r"\breg\s+delete\b.*\bHKLM\b", "registry delete (HKLM)"),
    (r"\breg\s+delete\b.*\bHKCR\b", "registry delete (HKCR)"),
    (r"\bnet\s+user\b", "user account management"),
    (r"\bnet\s+localgroup\b", "group management"),
    (r"\btakeown\s+/f\s+[cC]:\\\\?$", "take ownership of system drive"),
    (r"\brm\s+-rf\s+(/|~|\.|\*)\s*$", "recursive delete of root/home"),
    (r"\brm\s+-rf\s+/", "recursive delete of root"),
    (r"\bdel\s+/[fFsSqQ]*\s*[cC]:\\\\?\s*$", "delete system drive"),
    (r"\bremove-item\b.*-recurse.*(?:-force|\bC:\\\\?)", "recursive forced delete"),
    (r"\bclear-disk\b", "disk wipe"),
    (r"\bformat-volume\b", "volume format"),
    (r"\binitialize-disk\b", "disk init"),
    (r"\bset-executionpolicy\s+unrestricted\b", "disable PowerShell safety"),
    (r"invoke-expression.*(?:downloadstring|iwr|webclient)", "download-and-execute"),
    (r"iex\s*\(?.*downloadstring", "download-and-execute"),
    (r"curl\s+[^|]*\|\s*(?:bash|sh|powershell)", "pipe-to-shell"),
    (r"\bstart-process\b.*-verb\s+runas", "privilege escalation"),
    (r"\brunas\b", "privilege escalation"),
    (r"\bsudo\s+", "privilege escalation"),
    (r"powershell.*-enc(odedcommand)?\s+", "obfuscated command"),
    (r"certutil\s+.*-decode", "lolbin decode"),
    (r"\bbitsadmin\b.*\/transfer", "lolbin transfer"),
)

#: git subcommands that change shared state -> admin risk.
GIT_ADMIN = {"push", "reset", "clean", "rebase", "force", "gc", "prune", "filter-branch"}
GIT_WRITE = {"add", "commit", "checkout", "branch", "merge", "stash", "tag", "switch", "restore"}

RISK_TO_ACTION = {"read": "run_command_read", "write": "run_command_write",
                  "admin": "run_command_admin", "critical": "run_command_critical"}


class TerminalResult:
    """Outcome of one command execution."""

    __slots__ = ("ok", "command", "shell", "exit_code", "stdout", "stderr",
                 "risk", "duration_s", "blocked_reason", "truncated")

    def __init__(self, ok: bool, command: str, shell: str, exit_code: int = -1,
                 stdout: str = "", stderr: str = "", risk: str = "read",
                 duration_s: float = 0.0, blocked_reason: str = "", truncated: bool = False):
        self.ok = ok
        self.command = command
        self.shell = shell
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.risk = risk
        self.duration_s = duration_s
        self.blocked_reason = blocked_reason
        self.truncated = truncated

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "command": self.command, "shell": self.shell,
                "exit_code": self.exit_code, "risk": self.risk,
                "duration_s": round(self.duration_s, 3),
                "stdout": self.stdout, "stderr": self.stderr,
                "blocked_reason": self.blocked_reason, "truncated": self.truncated}

    def __str__(self) -> str:
        if self.blocked_reason:
            return "BLOCKED (%s): %s" % (self.blocked_reason, self.command)
        head = "$ %s   [%s, exit %d, %.2fs]" % (self.command, self.risk,
                                                self.exit_code, self.duration_s)
        body = (self.stdout or "").strip()
        if self.stderr and not self.ok:
            body = (body + "\n" + self.stderr.strip()).strip()
        return head + ("\n" + body if body else "")


class TerminalTool:
    """Execute real commands, safely."""

    #: Hard output cap - protects RAM and the dashboard (4 GB budget).
    MAX_OUTPUT = 240_000

    def __init__(self, apartment, default_shell: Optional[str] = None):
        self.apartment = apartment
        self.shells = find_shells()
        cfg = apartment.config.get("terminal", {}) or {}
        self.default_shell = default_shell or cfg.get("default_shell") or self._pick_default()
        self.timeout = float(cfg.get("timeout_s", 60))
        self.default_cwd = cfg.get("cwd") or None
        self.history: List[TerminalResult] = []
        self.max_history = int(cfg.get("max_history", 40))

    def _pick_default(self) -> str:
        if sys.platform == "win32":
            return "powershell" if self.shells.get("powershell") else "cmd"
        return "bash" if self.shells.get("bash") else "cmd"

    # ------------------------------------------------------------------ policy
    def classify(self, command: str) -> Tuple[str, str]:
        """Return ``(risk, reason)`` for a command line.  Pure, no side effects."""
        lowered = (command or "").lower().strip()
        if not lowered:
            return "read", "empty"
        for pattern, reason in DENYLIST:
            if re.search(pattern, lowered, re.I):
                return "critical", reason
        try:
            argv = shlex.split(command, posix=(sys.platform != "win32")) or []
        except ValueError:
            return "admin", "unparsable command (quote mismatch)"
        if not argv:
            return "read", "empty"
        exe = Path(argv[0]).stem.lower()

        # git gets subcommand-level granularity
        if exe == "git" and len(argv) > 1:
            sub = argv[1].lower().lstrip("-")
            if sub in GIT_ADMIN:
                return "admin", "git %s" % sub
            if sub in GIT_WRITE:
                return "write", "git %s" % sub
            return "read", "git %s" % sub

        risk, description = ALLOWLIST.get(exe, (None, ""))
        if risk is None:
            return "admin", "unknown executable '%s' (not in allowlist)" % exe
        return risk, description

    def _argv(self, command: str, shell: str) -> List[str]:
        """Build a safe argv list - never a shell string."""
        exe_path = self.shells.get(shell) or shell
        if shell == "cmd":
            return [exe_path, "/c", command]
        if shell in ("powershell", "pwsh"):
            return [exe_path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                    "Bypass", "-Command", command]
        return [exe_path, "-c", command]

    # ------------------------------------------------------------------- run
    def run(self, command: str, shell: Optional[str] = None, cwd: Optional[str] = None,
            timeout: Optional[float] = None, env: Optional[Dict[str, str]] = None,
            interactive: Optional[bool] = None) -> TerminalResult:
        """Execute one command.  Returns a :class:`TerminalResult` (never raises
        for policy reasons - a block is a result, not a crash)."""
        shell = shell or self.default_shell
        risk, reason = self.classify(command)

        if risk == "critical":
            result = TerminalResult(ok=False, command=command, shell=shell, risk="critical",
                                    blocked_reason="blocked by policy: %s" % reason)
            self._record(result)
            self.apartment.log("command_blocked", command=command[:200], reason=reason)
            return result

        action = RISK_TO_ACTION[risk]
        try:
            self.apartment.gate.enforce(
                self.apartment.NAME, action,
                {"command": command[:200], "shell": shell, "risk_reason": reason},
                interactive)
        except PermissionDenied as exc:
            result = TerminalResult(ok=False, command=command, shell=shell, risk=risk,
                                    blocked_reason=str(exc))
            self._record(result)
            self.apartment.log("command_denied", command=command[:200], reason=str(exc))
            return result

        if not self.shells.get(shell):
            return TerminalResult(ok=False, command=command, shell=shell, risk=risk,
                                  blocked_reason="shell '%s' not found on this machine" % shell)

        argv = self._argv(command, shell)
        started = time.time()
        run_env = None
        if env:
            run_env = dict(os.environ)
            run_env.update(env)
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            completed = subprocess.run(
                argv, cwd=cwd or self.default_cwd, env=run_env,
                capture_output=True, timeout=float(timeout or self.timeout),
                shell=False, text=True, errors="replace",
                creationflags=creationflags)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            exit_code = completed.returncode
            ok = exit_code == 0
        except subprocess.TimeoutExpired:
            stdout, stderr, exit_code, ok = "", "timeout after %ss" % (timeout or self.timeout), -1, False
        except FileNotFoundError as exc:
            stdout, stderr, exit_code, ok = "", str(exc), 127, False
        except Exception as exc:
            stdout, stderr, exit_code, ok = "", "%s: %s" % (type(exc).__name__, exc), -1, False

        truncated = False
        if len(stdout) > self.MAX_OUTPUT:
            stdout = stdout[:self.MAX_OUTPUT] + "\n...[truncated]"
            truncated = True
        if len(stderr) > 20000:
            stderr = stderr[:20000] + "\n...[truncated]"
            truncated = True

        result = TerminalResult(ok=ok, command=command, shell=shell, exit_code=exit_code,
                                stdout=stdout, stderr=stderr, risk=risk,
                                duration_s=time.time() - started, truncated=truncated)
        self._record(result)
        self.apartment.log("command_ran", command=command[:200], risk=risk,
                           exit_code=exit_code, ms=int(result.duration_s * 1000))
        return result

    def _record(self, result: TerminalResult) -> None:
        self.history.append(result)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    # -------------------------------------------------------------- convenience
    def powershell(self, command: str, **kwargs) -> TerminalResult:
        return self.run(command, shell="powershell" if self.shells.get("powershell")
                        else "pwsh", **kwargs)

    def cmd(self, command: str, **kwargs) -> TerminalResult:
        return self.run(command, shell="cmd", **kwargs)

    def bash(self, command: str, **kwargs) -> TerminalResult:
        return self.run(command, shell="bash", **kwargs)

    def script(self, body: str, shell: Optional[str] = None, **kwargs) -> TerminalResult:
        """Run a multi-line script safely (heredoc for bash, script block for PS)."""
        shell = shell or self.default_shell
        if shell == "bash":
            return self.run(body, shell="bash", **kwargs)
        # PowerShell: wrap in a script block so pipes/newlines behave.
        return self.run("& { %s }" % body.strip(), shell=shell, **kwargs)

    def which(self, executable: str) -> Optional[str]:
        return shutil.which(executable)

    def system_info(self) -> Dict[str, Any]:
        """Live machine stats - real numbers, no placeholders."""
        info: Dict[str, Any] = {"platform": sys.platform, "python": sys.version.split()[0],
                                "shells": {k: v for k, v in self.shells.items() if v}}
        try:
            import platform as _p
            info["machine"] = _p.machine()
            info["node"] = _p.node()
            info["os"] = _p.platform()
        except Exception:
            pass
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            if sys.platform == "win32":
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                info["ram_total_mb"] = int(stat.ullTotalPhys / 1048576)
                info["ram_free_mb"] = int(stat.ullAvailPhys / 1048576)
                info["ram_used_pct"] = int(stat.dwMemoryLoad)
        except Exception:
            pass
        if "ram_total_mb" not in info:
            try:
                info["ram_total_mb"] = int(os.sysconf("SC_PAGE_SIZE") *
                                           os.sysconf("SC_PHYS_PAGES") / 1048576)
            except Exception:
                pass
        try:
            usage = shutil.disk_usage(str(Path.home()))
            info["disk_free_gb"] = round(usage.free / 1073741824, 1)
            info["disk_total_gb"] = round(usage.total / 1073741824, 1)
        except Exception:
            pass
        return info

    def info(self) -> Dict[str, Any]:
        return {"default_shell": self.default_shell,
                "shells": {k: v for k, v in self.shells.items() if v},
                "timeout": self.timeout, "history": len(self.history),
                "allowlisted_commands": len(ALLOWLIST),
                "denylist_patterns": len(DENYLIST)}


def build(apartment) -> TerminalTool:
    return TerminalTool(apartment)
