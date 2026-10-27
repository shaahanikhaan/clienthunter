"""Send-time scheduler - when a message actually gets read.

Why this exists
---------------
Every platform has its own rhythm, and Pakistani business hours are their own
beast: Friday Jummah shuts most shops down for 90 minutes, Ramadan moves the
whole evening later, and a restaurant manager is unreachable at 8 pm on a
Saturday while a clinic's receptionist is free at 10 am on a Tuesday.

Nothing here needs a clock service or an API.  It is pure ``datetime`` plus the
platform windows measured in the field, so it works offline and on Windows
(``zoneinfo`` is used when present, otherwise a fixed UTC+5 Karachi offset).

    >>> from factory.scheduler import best_time, should_send_now
    >>> should_send_now("whatsapp")          # doctest: +SKIP
    (True, 'inside 17-20 window')
    >>> best_time("email", category="clinic")  # doctest: +SKIP
    1730023200.0
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "now", "in_window", "next_window", "best_time", "should_send_now",
    "plan_batch", "jummah_break", "PLATFORM_WINDOWS", "CATEGORY_WINDOWS",
]

# --------------------------------------------------------------------------- #
# Timezone: Asia/Karachi, without depending on the tzdata package
# --------------------------------------------------------------------------- #
_KARACHI = None


def _tz():
    global _KARACHI
    if _KARACHI is None:
        try:
            from zoneinfo import ZoneInfo
            _KARACHI = ZoneInfo("Asia/Karachi")
        except Exception:
            _KARACHI = timezone(timedelta(hours=5), "PKT")
    return _KARACHI


def now() -> datetime:
    """Current time in Karachi."""
    return datetime.now(_tz())


def _stamp(moment: datetime) -> float:
    return float(moment.timestamp())


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
#: (start_hour, end_hour) blocks that produce replies, per platform.
PLATFORM_WINDOWS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "whatsapp":   ((10, 12), (17, 20)),
    "email":      ((9, 11), (15, 16)),
    "linkedin":   ((9, 11), (14, 16)),
    "facebook":   ((11, 13), (19, 21)),
    "instagram":  ((12, 14), (20, 22)),
    "github":     ((10, 12), (15, 17)),
    "call":       ((11, 13), (16, 18)),
    "sms":        ((10, 12), (17, 19)),
}

#: Recipient-type overrides.  A restaurant owner is plating food at 8 pm; a
#: clinic receptionist is free right after Fajr rush.  Hours are inclusive
#: start, exclusive end.
CATEGORY_WINDOWS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "restaurant":     ((15, 17),),                 # after lunch, before dinner
    "cafe":           ((15, 17),),
    "hotel":          ((10, 12), (16, 18)),
    "clinic":         ((9, 11), (20, 21)),
    "hospital":       ((9, 11), (15, 17)),
    "pharmacy":       ((10, 12), (20, 22)),
    "salon":          ((11, 13), (19, 21)),
    "gym":            ((7, 9), (21, 22)),
    "school":         ((8, 10), (14, 15)),
    "college":        ((9, 11), (14, 16)),
    "real_estate":    ((11, 13), (18, 20)),
    "contractor":     ((9, 11), (18, 20)),
    "shop":           ((11, 13), (20, 21)),
    "garage":         ((10, 12), (18, 20)),
    "law_firm":       ((10, 12), (15, 17)),
    "accountant":     ((10, 12), (15, 17)),
    "it_company":     ((10, 12), (15, 17)),
    "ngo":            ((10, 12), (14, 16)),
}

#: Jummah: most Pakistani businesses pause roughly 12:45-14:15 on Friday.
JUMMAH = (12.75, 14.25)
#: Friday is a half day; Saturday is a working day here.
FRIDAY = 4            # Monday == 0

RAMADAN_DEFAULT = False
_ramadan = RAMADAN_DEFAULT


def set_ramadan(active: bool) -> None:
    """Ramadan mode: shift evening windows later and add a post-Iftar block."""
    global _ramadan
    _ramadan = bool(active)


def is_ramadan() -> bool:
    return _ramadan


def jummah_break(moment: Optional[datetime] = None) -> bool:
    """True during Friday prayers - do not send, nobody is reading."""
    moment = moment or now()
    if moment.weekday() != FRIDAY:
        return False
    hour = moment.hour + moment.minute / 60.0
    return JUMMAH[0] <= hour < JUMMAH[1]


def _windows(platform: str, category: str = "") -> Tuple[Tuple[float, float], ...]:
    """Platform windows, overridden by the recipient's business type, then
    shifted an hour later during Ramadan."""
    key = (platform or "").lower()
    base: Sequence[Tuple[int, int]] = CATEGORY_WINDOWS.get(
        (category or "").lower()) or PLATFORM_WINDOWS.get(key, ((10, 12), (17, 19)))
    shift = 1.0 if _ramadan else 0.0
    windows = tuple((float(start) + shift, float(end) + shift) for start, end in base)
    if _ramadan:
        windows = windows + ((21.5, 23.5),)      # post-Iftar / post-Taraweeh
    return windows


def in_window(platform: str, category: str = "",
              moment: Optional[datetime] = None) -> bool:
    """Is *right now* a good time to send on this platform?"""
    moment = moment or now()
    if jummah_break(moment):
        return False
    hour = moment.hour + moment.minute / 60.0
    return any(start <= hour < end for start, end in _windows(platform, category))


def next_window(platform: str, category: str = "",
                moment: Optional[datetime] = None) -> Tuple[datetime, str]:
    """The next moment a message should go out, and why it opens then."""
    moment = moment or now()
    windows = _windows(platform, category)
    for day_offset in range(0, 3):                 # look at most 3 days ahead
        day = (moment + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for start, end in sorted(windows):
            candidate = day + timedelta(hours=start)
            if candidate <= moment:
                continue
            if jummah_break(candidate + timedelta(minutes=15)):
                continue
            label = "%s window" % (category or platform)
            return candidate, "%s %02d:00-%02d:00" % (label, int(start), int(end))
    fallback = (moment + timedelta(days=1)).replace(hour=10, minute=0, second=0)
    return fallback, "default 10:00 slot"


def best_time(platform: str, category: str = "", lead_id: Any = None,
              moment: Optional[datetime] = None) -> float:
    """Exact epoch to send: next open window + a stable per-lead jitter.

    The jitter is a hash of ``lead_id`` so the same lead always gets the same
    slot (retries do not reshuffle the queue) while a batch still trickles out
    instead of firing 20 identical messages in the same second.
    """
    moment = moment or now()
    if in_window(platform, category, moment):
        window_start, window_end = _current_window(platform, category, moment)
    else:
        opening, _ = next_window(platform, category, moment)
        span = [w for w in _windows(platform, category)
                if int(w[0]) == opening.hour]
        window_start = opening
        window_end = opening + timedelta(hours=(span[0][1] - span[0][0]) if span else 2)
    span_seconds = max(60.0, (window_end - window_start).total_seconds() - 300)
    seed = hashlib.sha1(str(lead_id if lead_id is not None else "").encode(
        "utf-8")).digest()
    jitter = (seed[0] * 256 + seed[1]) / 65535.0
    return _stamp(window_start + timedelta(seconds=jitter * span_seconds))


def _current_window(platform: str, category: str,
                    moment: datetime) -> Tuple[datetime, datetime]:
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    hour = moment.hour + moment.minute / 60.0
    for start, end in _windows(platform, category):
        if start <= hour < end:
            return day + timedelta(hours=start), day + timedelta(hours=end)
    return moment, moment + timedelta(hours=1)


def should_send_now(platform: str, category: str = "",
                    moment: Optional[datetime] = None) -> Tuple[bool, str]:
    """``(ok, human reason)`` - the reason is shown in the dashboard."""
    moment = moment or now()
    if jummah_break(moment):
        return False, "Jummah break (12:45-14:15) - sab log namaz mein hain"
    hour = moment.hour + moment.minute / 60.0
    for start, end in _windows(platform, category):
        if start <= hour < end:
            return True, "inside %02d:00-%02d:00 window" % (int(start), int(end))
    opening, why = next_window(platform, category, moment)
    return False, "bahar ka time - agla slot %s (%s)" % (
        opening.strftime("%a %H:%M"), why)


# --------------------------------------------------------------------------- #
# Batch planning
# --------------------------------------------------------------------------- #
def plan_batch(platform: str, count: int, daily_cap: int,
               category: str = "", moment: Optional[datetime] = None,
               min_gap_minutes: int = 7) -> List[float]:
    """Spread ``count`` sends across today's windows, honouring a daily cap.

    Returns a list of epochs (already truncated to ``daily_cap``).  Messages are
    spaced at least ``min_gap_minutes`` apart so the sending pattern looks like
    a person, not a script.
    """
    moment = moment or now()
    cap = max(0, min(int(count), int(daily_cap)))
    if not cap:
        return []
    windows = _windows(platform, category)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    slots: List[datetime] = []
    for start, end in sorted(windows):
        cursor = day + timedelta(hours=start)
        stop = day + timedelta(hours=end) - timedelta(minutes=min_gap_minutes)
        while cursor <= stop and len(slots) < cap:
            if cursor > moment and not jummah_break(cursor):
                slots.append(cursor)
            cursor += timedelta(minutes=min_gap_minutes)
    day_offset = 1
    while len(slots) < cap and day_offset < 4:      # spill into the next days
        future = (moment + timedelta(days=day_offset)).replace(
            hour=int(windows[0][0]), minute=0, second=0, microsecond=0)
        slots.append(future + timedelta(minutes=min_gap_minutes * len(slots)))
        day_offset += 1
    # strict ordering + a little human jitter (stable per position, not random)
    ordered = sorted(set(slots))
    jittered = [slot + timedelta(minutes=(index * 37) % 4)
                for index, slot in enumerate(ordered)]
    return [_stamp(slot) for slot in sorted(jittered)[:cap]]


def summary(platform: str = "", category: str = "") -> Dict[str, Any]:
    """What the dashboard shows: current verdict + today's remaining slots."""
    moment = now()
    platforms = [platform] if platform else sorted(PLATFORM_WINDOWS)
    rows = []
    for name in platforms:
        ok, why = should_send_now(name, category, moment)
        opening, _ = next_window(name, category, moment)
        rows.append({"platform": name, "send_now": ok, "reason": why,
                     "next_slot": opening.strftime("%a %H:%M"),
                     "windows": ["%02d-%02d" % (int(s), int(e))
                                 for s, e in _windows(name, category)]})
    return {"now": moment.strftime("%Y-%m-%d %H:%M %Z"),
            "ramadan": _ramadan,
            "jummah": jummah_break(moment),
            "rows": rows}
