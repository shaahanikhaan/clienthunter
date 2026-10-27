"""Deliverability checks with **no API and no dependency**.

Sending cold email to addresses that bounce is the fastest way to burn a
domain's reputation, and every "email verification" service wants an API key.
Two checks do almost all the work, and both are doable locally:

1. **Does the domain accept mail at all?**  A DNS MX query answers that. There
   is no DNS in the standard library, so this module speaks DNS itself: a
   ~120-line UDP resolver that builds the query packet and walks the answer
   section (with the usual compression-pointer handling). It asks the machine's
   configured resolvers, then falls back to 1.1.1.1 / 8.8.8.8.

2. **Is the address worth sending to?**  Role mailboxes (``info@``, ``sales@``),
   free providers and obvious typos get a confidence score, and Pakistani phone
   numbers are normalised to ``+923XXXXXXXXX`` so WhatsApp can actually reach
   them. This part is pure string work.

Optional tier-2: ``phonenumbers`` (2.6 MB wheel, zero dependencies) makes phone
validation exact; ``dnspython`` would replace the hand-rolled resolver. Both are
used only if already installed.

Nothing here sends an email or a probe packet to the target - MX lookup is
reconnaissance, not contact, which keeps it on the polite side of the line.
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

#: Mailboxes that exist at nearly every SME and convert reasonably.
ROLE_OK = ("info", "hello", "contact", "sales", "admin", "owner", "manager",
           "marketing", "support", "enquiries", "inquiries", "office")
#: Mailboxes that must never receive a cold pitch (undeliverable or hostile).
ROLE_BAD = ("noreply", "no-reply", "donotreply", "do-not-reply", "no_reply",
            "bounce", "bounces", "mailer-daemon", "postmaster", "hostmaster",
            "abuse", "spam", "unsubscribe", "dmarc", "dmarc-reports", "null")
#: Deliverable, but a weak target: the mailbox works, it is just not the buyer.
ROLE_WEAK = ("hr", "jobs", "careers", "recruitment", "billing", "accounts",
             "payroll", "legal", "privacy", "compliance", "it", "webmaster",
             "feedback", "complaints", "help")
#: Disposable / throwaway providers - a lead here is not a lead.
DISPOSABLE = ("mailinator.com", "guerrillamail.com", "10minutemail.com",
              "tempmail.com", "throwawaymail.com", "yopmail.com", "trashmail.com",
              "sharklasers.com", "getnada.com", "maildrop.cc", "dispostable.com")
#: Free providers (a business using these is usually a one-person shop).
FREE_MAIL = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
             "icloud.com", "aol.com", "protonmail.com", "proton.me", "gmx.com",
             "mail.com", "yandex.com", "zoho.com")

#: Pakistani mobile prefixes -> operator (real allocations).
PK_MOBILE_PREFIXES = {
    "30": "Jazz", "31": "Zong", "32": "Warid/Jazz", "33": "Ufone",
    "34": "Telenor", "35": "SCOM", "36": "Zong", "37": "Warid/Jazz",
    "38": "Ufone", "39": "Zong", "40": "Jazz", "41": "Telenor",
    "42": "Ufone", "43": "Ufone", "44": "Ufone", "45": "Ufone",
    "46": "Telenor", "47": "Ufone", "48": "Ufone", "49": "Ufone",
}

CACHE_TTL = 6 * 3600        # MX answers are cached 6 h (RAM-cheap, polite)


# --------------------------------------------------------------------------- #
# A minimal DNS client (stdlib only)
# --------------------------------------------------------------------------- #
def _system_resolvers() -> List[str]:
    found: List[str] = []
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1:
                        found.append(parts[1])
    except Exception:
        pass
    if os.name == "nt":                      # Windows: ask ipconfig-less fallback
        found.extend(_windows_resolvers())
    return found or ["1.1.1.1", "8.8.8.8"]


def _windows_resolvers() -> List[str]:
    """Read DNS servers from the registry without a third-party package."""
    try:
        import winreg
    except Exception:
        return []
    servers: List[str] = []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces")
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(key, sub) as handle:
                    value, _ = winreg.QueryValueEx(handle, "NameServer")
                    for server in str(value or "").replace(",", " ").split():
                        if server and server not in servers:
                            servers.append(server)
                    if not value:
                        dhcp, _ = winreg.QueryValueEx(handle, "DhcpNameServer")
                        for server in str(dhcp or "").replace(",", " ").split():
                            if server and server not in servers:
                                servers.append(server)
            except OSError:
                continue
        winreg.CloseKey(key)
    except Exception:
        return []
    return servers


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.strip(".").split("."):
        raw = label.encode("idna") if label else b""
        out.append(len(raw))
        out += raw
    out.append(0)
    return bytes(out)


def _decode_name(data: bytes, offset: int) -> Tuple[str, int]:
    """Read a (possibly compressed) domain name.  Returns (name, next_offset)."""
    labels: List[str] = []
    jumped = False
    end = offset
    seen = 0
    while True:
        if offset >= len(data) or seen > 32:
            break
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:                       # compression pointer
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("latin-1", "replace"))
        offset += length
        if not jumped:
            end = offset
        seen += 1
    return ".".join(label for label in labels if label), (end if jumped else end)


def dns_query(name: str, qtype: int, server: Optional[str] = None,
              timeout: float = 3.0, retries: int = 2) -> List[Tuple[str, int, int, str]]:
    """Raw DNS query over UDP.  Returns ``[(name, type, class, rdata)]``.

    ``rdata`` is text for MX/A/AAAA/TXT/CNAME answers (MX rdata is
    ``"preference host"``).  Never raises - returns ``[]`` on any failure.
    """
    servers = [server] if server else _system_resolvers()
    transaction = random.randint(0, 0xFFFF)
    message = struct.pack(">HHHHHH", transaction, 0x0100, 1, 0, 0, 0)
    message += _encode_name(name) + struct.pack(">HH", qtype, 1)
    for resolver in servers:
        for attempt in range(retries + 1):
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(timeout)
                sock.sendto(message, (resolver, 53))
                payload, _ = sock.recvfrom(4096)
                return _parse_dns(payload, qtype)
            except Exception:
                if attempt < retries:
                    time.sleep(0.4 * (attempt + 1))
                continue
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
    return []


def _parse_dns(payload: bytes, qtype: int) -> List[Tuple[str, int, int, str]]:
    if len(payload) < 12:
        return []
    qdcount, ancount = struct.unpack(">HH", payload[4:8])
    offset = 12
    for _ in range(qdcount):
        _, offset = _decode_name(payload, offset)
        offset += 4
    records: List[Tuple[str, int, int, str]] = []
    for _ in range(ancount):
        name, offset = _decode_name(payload, offset)
        if offset + 10 > len(payload):
            break
        rtype, rclass, _ttl, rdlength = struct.unpack(">HHIH", payload[offset:offset + 10])
        offset += 10
        rdata = payload[offset:offset + rdlength]
        if rtype == 15:                                   # MX
            preference = struct.unpack(">H", rdata[:2])[0]
            exchange, _ = _decode_name(payload, offset + 2)
            records.append((name, rtype, rclass, "%d %s" % (preference, exchange)))
        elif rtype in (1, 28) and rdlength in (4, 16):    # A / AAAA
            address = socket.inet_ntop(socket.AF_INET if rdlength == 4 else socket.AF_INET6,
                                       rdata)
            records.append((name, rtype, rclass, address))
        elif rtype in (16, 5):                            # TXT / CNAME
            if rtype == 5:
                text, _ = _decode_name(payload, offset)
            else:
                text = rdata.decode("latin-1", "replace")
            records.append((name, rtype, rclass, text))
        else:
            records.append((name, rtype, rclass, ""))
        offset += rdlength
    return [record for record in records if record[1] == qtype]


def mx_records(domain: str, timeout: float = 3.0) -> List[Tuple[int, str]]:
    """MX hosts for a domain, best (lowest preference) first."""
    answers = dns_query(domain, 15, timeout=timeout)
    rows: List[Tuple[int, str]] = []
    for _name, _type, _cls, rdata in answers:
        parts = rdata.split(None, 1)
        if len(parts) == 2:
            try:
                rows.append((int(parts[0]), parts[1].rstrip(".")))
            except ValueError:
                continue
    rows.sort(key=lambda row: row[0])
    return rows


# --------------------------------------------------------------------------- #
# Phone handling
# --------------------------------------------------------------------------- #
def normalise_pk_phone(raw: str) -> Dict[str, Any]:
    """Normalise a Pakistani number to E.164 and classify it.

    Handles ``0300-1234567``, ``+92 300 1234567``, ``00923001234567``,
    ``(021) 1234567`` (landline) and the common ``92-300-1234567`` form.
    """
    text = str(raw or "")
    digits = re.sub(r"\D", "", text)
    if digits.startswith("0092"):
        digits = digits[2:]
    number = ""
    kind = "unknown"
    if digits.startswith("92") and len(digits) == 12:
        number = "+" + digits
    elif digits.startswith("0") and len(digits) == 11:
        number = "+92" + digits[1:]
    elif len(digits) == 10 and digits[0] == "3":
        number = "+92" + digits
    elif len(digits) == 10 and digits[0] in "2456789":      # landline w/o city code
        number = "+9221" + digits if len(digits) == 10 else ""
        kind = "landline"
    elif len(digits) == 11 and digits.startswith("21"):     # Karachi landline
        number = "+92" + digits
        kind = "landline"
    if not number:
        return {"input": raw, "valid": False, "e164": "", "kind": "unknown",
                "operator": "", "whatsapp_eligible": False, "reason": "unrecognised"}
    local = number[3:]
    if len(local) == 10 and local.startswith("3"):
        kind = "mobile"
        operator = PK_MOBILE_PREFIXES.get(local[:2], "")
    else:
        kind = kind if kind == "landline" else "landline"
        operator = ""
    return {"input": raw, "valid": True, "e164": number, "kind": kind,
            "operator": operator, "whatsapp_eligible": kind == "mobile",
            "reason": ""}


def phone_report(raw: str) -> Dict[str, Any]:
    """stdlib report, upgraded with ``phonenumbers`` when it is installed."""
    result = normalise_pk_phone(raw)
    try:
        import phonenumbers  # type: ignore
        parsed = phonenumbers.parse(str(raw), "PK")
        result["lib"] = "phonenumbers"
        result["valid"] = bool(phonenumbers.is_valid_number(parsed))
        result["e164"] = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164) or result["e164"]
        result["kind"] = ("mobile" if phonenumbers.number_type(parsed) in
                          (phonenumbers.PhoneNumberType.MOBILE,
                           phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)
                          else "landline")
        result["whatsapp_eligible"] = result["kind"] == "mobile" and result["valid"]
    except Exception:
        result.setdefault("lib", "stdlib")
    return result


# --------------------------------------------------------------------------- #
# Email confidence
# --------------------------------------------------------------------------- #
def email_report(address: str, check_mx: bool = True,
                 cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Score one address: syntax, role, provider, and MX deliverability."""
    address = (address or "").strip().strip("<>").lower()
    report: Dict[str, Any] = {"email": address, "valid_syntax": False,
                              "domain": "", "role": "", "provider": "",
                              "mx": [], "mx_ok": None, "score": 0.0,
                              "sendable": False, "reasons": []}
    if not EMAIL_RE.match(address) or address.count("@") != 1:
        report["reasons"].append("bad syntax")
        return report
    report["valid_syntax"] = True
    local, domain = address.split("@")
    report["domain"] = domain
    report["role"] = local.split("+")[0]
    score = 0.5
    if local in ROLE_BAD or local.startswith(("no-reply", "noreply")):
        score -= 0.45
        report["reasons"].append("do-not-reply mailbox")
    elif local in ROLE_OK:
        score += 0.15
        report["reasons"].append("standard business mailbox")
    elif local in ROLE_WEAK:
        score -= 0.12
        report["reasons"].append("back-office mailbox (works, weak buyer)")
    else:
        score += 0.05
    if domain in DISPOSABLE:
        score -= 0.6
        report["reasons"].append("disposable provider")
    elif domain in FREE_MAIL:
        score -= 0.12
        report["reasons"].append("free provider (often a one-person shop)")
    else:
        score += 0.12
        report["reasons"].append("own domain")
    report["provider"] = "disposable" if domain in DISPOSABLE else (
        "free" if domain in FREE_MAIL else "business")

    if check_mx:
        rows = []
        if cache is not None:
            cached = cache.get(domain)
            if cached and time.time() - cached["at"] < CACHE_TTL:
                rows = cached["mx"]
        if not rows:
            rows = mx_records(domain)
            if cache is not None:
                cache[domain] = {"at": time.time(), "mx": rows}
        report["mx"] = [host for _pref, host in rows]
        report["mx_ok"] = bool(rows)
        if rows:
            score += 0.25
            report["reasons"].append("MX: %s" % rows[0][1])
        else:
            score -= 0.5
            report["reasons"].append("no MX - domain cannot receive mail")
    report["score"] = round(max(0.0, min(1.0, score)), 3)
    report["sendable"] = bool(report["valid_syntax"] and report["mx_ok"] is not False
                              and report["score"] >= 0.45
                              and domain not in DISPOSABLE)
    return report


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #
class VerifyTool:
    """Apartment-facing wrapper: caches, audits, and never raises."""

    def __init__(self, apartment, check_mx: bool = True):
        self.apartment = apartment
        self.check_mx = bool(check_mx)
        self._mx_cache: Dict[str, Any] = {}
        self._mx_timeouts = 0
        self.stats = {"emails": 0, "phones": 0, "mx_lookups": 0, "sendable": 0}

    @property
    def mx_enabled(self) -> bool:
        """Auto-disables MX lookups when DNS is unreachable (offline / firewalled)."""
        return bool(self.check_mx) and self._mx_timeouts < 2

    def email(self, address: str, check_mx: Optional[bool] = None) -> Dict[str, Any]:
        use_mx = self.mx_enabled if check_mx is None else bool(check_mx) and self.mx_enabled
        result = email_report(address, check_mx=use_mx, cache=self._mx_cache)
        if use_mx and result.get("mx") is None and result.get("mx_ok") is None:
            self._mx_timeouts += 1
        elif use_mx:
            self._mx_timeouts = 0
        self.stats["emails"] += 1
        if use_mx:
            self.stats["mx_lookups"] += 1
        if result["sendable"]:
            self.stats["sendable"] += 1
        return result

    def emails(self, addresses, limit: int = 10) -> List[Dict[str, Any]]:
        rows = []
        seen = set()
        for address in list(addresses or [])[:limit]:
            key = str(address).lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(self.email(key))
        rows.sort(key=lambda row: -row["score"])
        return rows

    def phone(self, raw: str) -> Dict[str, Any]:
        self.stats["phones"] += 1
        return phone_report(raw)

    def phones(self, numbers, limit: int = 10) -> List[Dict[str, Any]]:
        rows, seen = [], set()
        for number in list(numbers or [])[:limit]:
            key = re.sub(r"\D", "", str(number))
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(self.phone(number))
        return rows

    def lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Score a whole lead: best email + best phone + an overall verdict."""
        emails = self.emails([lead.get("email")] if lead.get("email") else [], limit=3)
        phones = self.phones([lead.get("phone")] if lead.get("phone") else [], limit=3)
        best_email = emails[0] if emails else None
        best_phone = phones[0] if phones else None
        verdict = {
            "lead_id": lead.get("id"),
            "email": best_email,
            "phone": best_phone,
            "contactable": bool((best_email and best_email["sendable"]) or
                                (best_phone and best_phone["valid"] and
                                 best_phone["whatsapp_eligible"])),
            "preferred_channel": "whatsapp" if (best_phone and best_phone.get("valid")
                                                and best_phone.get("whatsapp_eligible"))
            else ("email" if best_email and best_email.get("sendable") else "none"),
        }
        self.apartment.log("verified", lead_id=lead.get("id"),
                           contactable=verdict["contactable"],
                           channel=verdict["preferred_channel"])
        return verdict

    def score_lead(self, emails: Optional[List[str]] = None,
                   phones: Optional[List[str]] = None,
                   website: str = "") -> Dict[str, Any]:
        """Rank every candidate contact path for one lead (batch-friendly)."""
        email_rows = self.emails(emails or [], limit=10)
        phone_rows = self.phones(phones or [], limit=10)
        sendable = [row for row in email_rows if row.get("sendable")]
        whatsapp = [row for row in phone_rows
                    if row.get("valid") and row.get("whatsapp_eligible")]
        if whatsapp:
            channel = "whatsapp"
        elif sendable:
            channel = "email"
        elif [row for row in phone_rows if row.get("valid")]:
            channel = "call"
        else:
            channel = "none"
        return {
            "emails": email_rows,
            "phones": phone_rows,
            "sendable_emails": [row["email"] for row in sendable],
            "whatsapp_numbers": [row["e164"] for row in whatsapp],
            "best_email": sendable[0]["email"] if sendable else "",
            "best_phone": (whatsapp[0]["e164"] if whatsapp else
                           (phone_rows[0]["e164"] if phone_rows else "")),
            "preferred_channel": channel,
            "contactable": channel != "none",
            "website": website,
        }

    def info(self) -> Dict[str, Any]:
        return {"check_mx": self.check_mx, "stats": dict(self.stats),
                "resolvers": _system_resolvers()[:3],
                "phone_backend": _phone_backend(), "cache_entries": len(self._mx_cache)}


def _phone_backend() -> str:
    try:
        import phonenumbers  # noqa: F401
        return "phonenumbers"
    except Exception:
        return "stdlib"


def build(apartment) -> VerifyTool:
    return VerifyTool(apartment)
