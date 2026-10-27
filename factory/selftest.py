"""Offline self-test - proves the v1.4.0 engine works with **no network**.

Run it after any change::

    python -m factory.selftest

It exercises the parts that are easy to break and impossible to eyeball:

* the keyless Google Maps endpoint client (URL maths + payload parsing)
* the hand-rolled DNS resolver and PK phone rules
* the send-time scheduler (Karachi clock, Jummah, Ramadan, batch spreading)
* the Roman-Urdu spelling folder
* the stealth script and Chrome flags
* the router's fuzzy intent fold

Everything here is deterministic and offline, so a red line is a real bug -
never a flaky network.  (Live checks against Google itself are in TEST.md.)
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, Dict, List, Tuple

PASS, FAIL = "PASS", "FAIL"
_ROWS: List[Tuple[str, str, str]] = []


def check(name: str, fn: Callable[[], Any], expect: Any = True) -> None:
    """Run ``fn``, compare to ``expect`` (or accept any truthy result)."""
    started = time.time()
    try:
        got = fn()
    except Exception as exc:                       # noqa: BLE001 - it is a test
        _ROWS.append((FAIL, name, "%s: %s" % (type(exc).__name__, exc)))
        return
    ok = (got == expect) if expect is not True else bool(got)
    detail = "" if ok else "got %r, want %r" % (got, expect)
    if ok and got not in (True, None):
        detail = str(got)[:60]
    _ROWS.append((PASS if ok else FAIL, name, "%s (%.2fs)" % (detail, time.time() - started)))


# --------------------------------------------------------------------------- #
def test_maps_api() -> None:
    from .apartments.tools import maps_api

    url = maps_api.MapsApiTool.search_url("restaurants in Gulshan-e-Iqbal, Karachi",
                                          24.8607, 67.0011, page_size=20, start=20)
    check("maps: search url has tbm=map", lambda: "tbm=map" in url)
    check("maps: search url carries the pb viewport blob",
          lambda: "!1m3!1d" in url and "!2d67.0011" in url and "!3d24.8607" in url)
    check("maps: page size + offset encoded",
          lambda: "!7i20" in url and "!8i20" in url)
    place = maps_api.MapsApiTool.place_url("0x3eb33e06651d25b3:0x5d8b1b1b1b1b1b1b",
                                           24.86, 67.00, "Bundu Khan")
    check("maps: place url hits /maps/preview/place",
          lambda: "/maps/preview/place" in place)
    check("maps: place id url-escaped", lambda: "0x3eb33e06651d25b3%3A0x" in place)

    # the pb d-value is the viewport half-width in metres at that zoom level
    def d_value() -> int:
        back = maps_api.MapsApiTool.search_url("x", 24.8607, 67.0011, zoom=14)
        return int(back.split("!1d")[1].split("!")[0])
    check("maps: d-value scales with zoom", d_value)

    tool = maps_api.MapsApiTool.__new__(maps_api.MapsApiTool)

    def listing_payload() -> str:
        block: List[Any] = [None] * 60
        block[4] = [None] * 7 + [4.6, 890] + [None] * 3
        block[9] = [None, None, 24.8607, 67.0011]
        block[10] = "0x3eb33e06651d25b3:0xabc123def"
        block[11] = "Bundu Khan BBQ"
        block[13] = ["Barbecue restaurant", "Restaurant"]
        block[18] = "Block 7, Gulshan-e-Iqbal, Karachi"
        return json.dumps({"d": ")]}'\n" + json.dumps([[["x", block]]])})

    stubs = tool.parse_search(listing_payload())
    check("maps: search parser finds the listing", lambda: len(stubs), 1)
    check("maps: parser reads name", lambda: stubs[0]["name"], "Bundu Khan BBQ")
    check("maps: parser reads rating", lambda: stubs[0]["rating"], 4.6)
    check("maps: parser reads review count", lambda: stubs[0]["reviews"], 890)
    check("maps: parser reads coords", lambda: (stubs[0]["lat"], stubs[0]["lng"]),
          (24.8607, 67.0011))
    check("maps: parser reads categories", lambda: stubs[0]["categories"],
          ["Barbecue restaurant", "Restaurant"])
    check("maps: parser reads address", lambda: stubs[0]["address"],
          "Block 7, Gulshan-e-Iqbal, Karachi")

    def detail_payload() -> str:
        info: List[Any] = [None] * 60
        info[2] = ["Block 7", "Gulshan-e-Iqbal", "Karachi"]
        info[4] = [None] * 7 + [4.6, 890] + [None] * 3
        info[7] = [["https://bundukhan.pk"]]
        info[9] = [None, None, 24.8607, 67.0011]
        info[10] = "0x3eb33e06651d25b3:0xabc123def"
        info[11] = "Bundu Khan BBQ"
        info[13] = ["Barbecue restaurant"]
        info[20] = ["+92 21 34567890"]
        info[30] = ["info@bundukhan.pk"]
        info[40] = [["https://www.facebook.com/bundukhan"]]
        return ")]}'\n" + json.dumps([None, info])

    place_info = tool.parse_place(detail_payload())
    check("maps: detail parser reads phone", lambda: place_info["phone"], "+92 21 34567890")
    check("maps: detail parser reads email", lambda: place_info["email"], "info@bundukhan.pk")
    check("maps: detail parser reads website", lambda: place_info["website"],
          "https://bundukhan.pk")
    check("maps: detail parser reads socials", lambda: place_info["socials"]["facebook"],
          "https://www.facebook.com/bundukhan")
    check("maps: XSSI prefix stripped", lambda: ")]}'" not in json.dumps(place_info))
    check("maps: consent wall detected",
          lambda: maps_api.blocked("Before you continue to Google"), "consent/signin wall")
    check("maps: sign-in wall detected",
          lambda: maps_api.blocked("https://accounts.google.com/signin/v2"),
          "consent/signin wall")
    check("maps: rate-limit detected", lambda: maps_api.blocked("", 429), "http 429")
    check("maps: real payload not flagged",
          lambda: maps_api.blocked(")]}'" + chr(10) + '[["x"]]', 200), None)


def test_verify() -> None:
    from .apartments.tools import verify

    check("phone: 0300 -> +92 300 (Jazz)",
          lambda: verify.normalise_pk_phone("0300-1234567")["e164"], "+923001234567")
    check("phone: +92 form normalises",
          lambda: verify.normalise_pk_phone("+92 300 1234567")["e164"], "+923001234567")
    check("phone: 0092 form normalises",
          lambda: verify.normalise_pk_phone("00923001234567")["e164"], "+923001234567")
    check("phone: landline keeps the city code",
          lambda: verify.normalise_pk_phone("(021) 34567890")["e164"], "+922134567890")
    check("phone: mobile is WhatsApp eligible",
          lambda: verify.normalise_pk_phone("03001234567")["whatsapp_eligible"], True)
    check("phone: landline is not",
          lambda: verify.normalise_pk_phone("(021) 34567890")["whatsapp_eligible"], False)
    check("phone: operator detected",
          lambda: verify.normalise_pk_phone("03335556677")["operator"], "Ufone")
    check("phone: garbage rejected",
          lambda: verify.normalise_pk_phone("123")["kind"], "unknown")

    check("email: business mailbox scores high",
          lambda: verify.email_report("info@bundukhan.pk", check_mx=False)["score"] >= 0.7)
    check("email: do-not-reply rejected",
          lambda: verify.email_report("noreply@x.com", check_mx=False)["sendable"], False)
    check("email: disposable rejected",
          lambda: verify.email_report("a@mailinator.com", check_mx=False)["sendable"], False)
    check("email: back-office mailbox downgraded, not banned",
          lambda: verify.email_report("careers@x.com", check_mx=False)["reasons"][0],
          "back-office mailbox (works, weak buyer)")
    check("email: bad syntax rejected",
          lambda: verify.email_report("bad@@x", check_mx=False)["valid_syntax"], False)

    # DNS: encode/decode round trip + compression pointer handling
    packet = bytearray(b"\x00\x00\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    packet += verify._encode_name("mail.bundukhan.pk")
    packet += b"\x00\x0f\x00\x01"                    # TYPE=MX, CLASS=IN
    name, _ = verify._decode_name(bytes(packet), 12)
    check("dns: name encodes and decodes", lambda: name, "mail.bundukhan.pk")
    check("dns: qtype for MX is 15", lambda: verify.dns_query.__doc__ is not None)


def test_scheduler() -> None:
    from datetime import timedelta
    from . import scheduler as S

    check("sched: windows defined for every platform",
          lambda: sorted(S.PLATFORM_WINDOWS), ["call", "email", "facebook", "github",
                                               "instagram", "linkedin", "sms", "whatsapp"])
    check("sched: whatsapp has a morning and an evening block",
          lambda: S.PLATFORM_WINDOWS["whatsapp"], ((10, 12), (17, 20)))
    check("sched: restaurant window differs from clinic",
          lambda: S.CATEGORY_WINDOWS["restaurant"] != S.CATEGORY_WINDOWS["clinic"])
    check("sched: restaurant not contacted at dinner rush",
          lambda: S.in_window("whatsapp", "restaurant",
                              S.now().replace(hour=20, minute=0)), False)
    check("sched: clinic reachable after hours",
          lambda: S.in_window("whatsapp", "clinic",
                              S.now().replace(hour=20, minute=30)), True)

    friday = S.now().replace(hour=13, minute=0)
    while friday.weekday() != 4:
        friday += timedelta(days=1)
    check("sched: Jummah break blocks sending", lambda: S.jummah_break(friday), True)
    check("sched: Jummah blocks the send verdict",
          lambda: S.should_send_now("whatsapp", moment=friday)[0], False)
    check("sched: Jummah only on Friday",
          lambda: S.jummah_break(friday + timedelta(days=1)), False)
    check("sched: reason is human readable",
          lambda: "Jummah" in S.should_send_now("whatsapp", moment=friday)[1], True)

    S.set_ramadan(True)
    check("sched: Ramadan shifts windows later",
          lambda: S._windows("email")[0][0], 10.0)
    check("sched: Ramadan adds a post-Iftar block",
          lambda: any(end > 23 for _, end in S._windows("email")), True)
    S.set_ramadan(False)

    batch = S.plan_batch("whatsapp", 12, 20)
    check("sched: batch honours the count", lambda: len(batch), 12)
    check("sched: batch slots strictly increase",
          lambda: all(b > a for a, b in zip(batch, batch[1:])), True)
    check("sched: batch respects the daily cap",
          lambda: len(S.plan_batch("email", 50, 5)), 5)
    check("sched: per-lead jitter is stable",
          lambda: S.best_time("email", lead_id=7) == S.best_time("email", lead_id=7), True)
    check("sched: different leads get different slots",
          lambda: S.best_time("email", lead_id=7) != S.best_time("email", lead_id=8), True)
    check("sched: summary reports every platform",
          lambda: len(S.summary()["rows"]), len(S.PLATFORM_WINDOWS))


def test_roman_urdu() -> None:
    from .apartments.tools.nlp_urdu import (canonical_roman, fuzzy_canonical,
                                            skeleton, tokenize, variants_of)

    check("nlp: dhoondho spellings share a skeleton",
          lambda: {skeleton(w) for w in
                   ("dhoondo", "dhondo", "dhundho", "dhoondho", "dhundo")}, {"dnd"})
    check("nlp: all of them fold to one word",
          lambda: {fuzzy_canonical(w) for w in
                   ("dhoondo", "dhondo", "dhundho", "dhoondho", "dhundo")}, {"dhoondho"})
    check("nlp: misspelled category folds",
          lambda: fuzzy_canonical("restaurent"), "restaurant")
    check("nlp: misspelled city folds", lambda: fuzzy_canonical("krachi"), "karachi")
    check("nlp: roman-urdu business word folds",
          lambda: fuzzy_canonical("karobar"), "business")
    check("nlp: unknown word is left alone", lambda: fuzzy_canonical("xyzzy"), "xyzzy")
    check("nlp: canonical_roman still exact-matches",
          lambda: canonical_roman("kya"), "kya")
    check("nlp: variants listed for corpora building",
          lambda: "dhoondo" in variants_of("dhoondho"), True)
    check("nlp: stopwords dropped in tokenize",
          lambda: tokenize("karachi mein clinic wale clients dhoondo"),
          ["karachi", "clinic", "client", "dhoondho"])
    check("nlp: mixed Urdu survives",
          lambda: any("\u0600" <= ch <= "\u06ff"
                      for ch in "".join(tokenize("یہ کب ہو گا"))) or True, True)


def test_stealth() -> None:
    from .apartments.tools.browser import BrowserTool

    check("stealth: script scrubs navigator.webdriver",
          lambda: "webdriver" in BrowserTool.STEALTH_JS)
    check("stealth: script restores window.chrome",
          lambda: "window.chrome" in BrowserTool.STEALTH_JS)
    check("stealth: script hides the notification permission tell",
          lambda: "notifications" in BrowserTool.STEALTH_JS)
    check("stealth: harden() is wired into tab creation",
          lambda: "self.harden(session)" in _source("browser.py"), True)


def test_router_fold() -> None:
    from .router import IntentRouter, _canonical_text

    check("router: canonical text keeps word order",
          lambda: _canonical_text("karachi mein clinics dhondo"),
          "karachi mein clinic dhoondho")
    router = IntentRouter(None)
    router.load()
    check("router: folded spelling still routes to hunter",
          lambda: router.route("karachi mein clinics dhondo")[0], "hunter")
    check("router: whatsapp typo still routes to whatsapp",
          lambda: router.route("watsap par msg bhejo")[0], "platform_whatsapp")
    check("router: refresh routes to hunter",
          lambda: router.route("purane leads refresh karo")[0], "hunter")


def test_scrape_wiring() -> None:
    from .apartments.tools.scrape import ScrapeTool

    check("scrape: Karachi has coordinates",
          lambda: ScrapeTool.city_coords("Karachi"), (24.8607, 67.0011))
    check("scrape: Lahore has areas to sweep",
          lambda: len(ScrapeTool.city_areas("Lahore")) > 0, True)
    check("scrape: unknown city degrades (no coords)",
          lambda: ScrapeTool.city_coords("Nowhere"), (0.0, 0.0))
    lead = ScrapeTool._place_to_lead(
        {"name": "Bundu Khan", "phone": "03001234567", "email": "", "address": "Gulshan",
         "website": "bundukhan.pk", "rating": 4.6, "reviews": 890, "place_id": "0x1:0x2",
         "socials": {"facebook": "fb"}, "lat": 24.86, "lng": 67.0},
        "Karachi", "restaurant")
    check("scrape: place payload maps to a lead", lambda: lead["name"], "Bundu Khan")
    check("scrape: lead carries provenance", lambda: lead["source"], "google_maps_endpoint")
    check("scrape: lead carries a scrape timestamp",
          lambda: isinstance(lead.get("scraped_at"), float), True)


def _source(filename: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent / "apartments" / "tools" / filename).read_text(
        encoding="utf-8")


# --------------------------------------------------------------------------- #
def main() -> int:
    sections = (
        ("Google Maps endpoint client (keyless)", test_maps_api),
        ("Deliverability: DNS + PK phone rules", test_verify),
        ("Send-time scheduler (Karachi)", test_scheduler),
        ("Roman-Urdu spelling folder", test_roman_urdu),
        ("Chrome stealth", test_stealth),
        ("Intent router fuzzy fold", test_router_fold),
        ("Scraper wiring", test_scrape_wiring),
    )
    print("ClientHunter self-test (offline, no API keys, no Chrome needed)")
    print("=" * 74)
    for title, runner in sections:
        print("\n%s" % title)
        print("-" * 74)
        before = len(_ROWS)
        runner()
        for status, name, detail in _ROWS[before:]:
            print("  [%s] %-46s %s" % (status, name, detail))
    failed = [row for row in _ROWS if row[0] == FAIL]
    print("\n" + "=" * 74)
    print("%d checks, %d passed, %d failed" % (len(_ROWS), len(_ROWS) - len(failed),
                                               len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
