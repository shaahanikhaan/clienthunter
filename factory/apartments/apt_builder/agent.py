"""apt_builder - builds the free demo website that closes deals.

"Build-first" is the strongest trust move in this market: instead of asking for
money up front, you hand over a real homepage mockup in 24 hours.  This
apartment generates it.

Output is a **single self-contained HTML file** - inline CSS, no CDN, no
JavaScript dependencies, no external fonts.  That matters because:

* it opens instantly on a Pakistani 3G connection (the demo is itself proof),
* it can be emailed or WhatsApped as one file,
* it cannot phone home or leak anything (no third-party requests at all).

Nothing here touches the network or executes anything: the "site" is a string
built from the lead's own data, escaped on the way in.
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment

#: Per-category palette + default section set.
CATEGORY_THEMES = {
    "restaurant": {"primary": "#b91c1c", "accent": "#f59e0b", "sections": ["menu", "about", "contact"]},
    "clinic": {"primary": "#0f766e", "accent": "#14b8a6", "sections": ["services", "doctors", "contact"]},
    "salon": {"primary": "#a21caf", "accent": "#f472b6", "sections": ["services", "gallery", "contact"]},
    "gym": {"primary": "#1d4ed8", "accent": "#f97316", "sections": ["programs", "pricing", "contact"]},
    "hotel": {"primary": "#065f46", "accent": "#d97706", "sections": ["rooms", "amenities", "contact"]},
    "real_estate": {"primary": "#334155", "accent": "#ca8a04", "sections": ["projects", "about", "contact"]},
    "school": {"primary": "#1e40af", "accent": "#facc15", "sections": ["programs", "admissions", "contact"]},
    "boutique": {"primary": "#be185d", "accent": "#fb7185", "sections": ["collection", "about", "contact"]},
    "pharmacy": {"primary": "#047857", "accent": "#34d399", "sections": ["services", "about", "contact"]},
    "auto_workshop": {"primary": "#475569", "accent": "#f97316", "sections": ["services", "about", "contact"]},
    "electronics": {"primary": "#0c4a6e", "accent": "#38bdf8", "sections": ["products", "services", "contact"]},
    "law_firm": {"primary": "#1f2937", "accent": "#a16207", "sections": ["practice", "team", "contact"]},
}
DEFAULT_THEME = {"primary": "#1e3a8a", "accent": "#f59e0b",
                 "sections": ["services", "about", "contact"]}

CATEGORY_COPY = {
    "restaurant": {"headline": "Ghar jaisa zaaiqa, ab online",
                   "cta": "WhatsApp par order karein",
                   "sub": "Taaza khana, 30 minute mein tayyar. Ab order karna aur bhi aasan hai."},
    "clinic": {"headline": "Aap ki sehat, hamari tarjeeh",
               "cta": "Appointment book karein",
               "sub": "Online appointment book karein - phone par wait karne ki zarurat nahi."},
    "salon": {"headline": "Aap ki khoobsurti, hamari pehchaan",
              "cta": "Booking ke liye message karein",
              "sub": "Bridal se le kar party makeup tak - sab kuch ek jagah."},
    "gym": {"headline": "Apni fitness ka safar aaj se shuru karein",
            "cta": "Free trial membership",
            "sub": "Modern equipment, certified trainers, aur aap ke liye ek plan."},
    "hotel": {"headline": "Aap ka araam, hamari zimmedari",
              "cta": "Room availability check karein",
              "sub": "Direct booking se behtar rate - koi commission nahi."},
    "real_estate": {"headline": "Aap ka sapno ka ghar yahin hai",
                    "cta": "Projects dekhein",
                    "sub": "Verified projects, transparent rates, aur poora support."},
    "school": {"headline": "Behtar kal ke liye behtar taleem",
               "cta": "Admission enquiry",
               "sub": "Experienced teachers, modern labs, aur individual attention."},
    "boutique": {"headline": "Har kapray mein aap ka andaaz",
                 "cta": "Collection dekhein",
                 "sub": "Naya collection har mahine - WhatsApp par catalogue hasil karein."},
}
DEFAULT_COPY = {"headline": "Professional. Fast. Mobile-ready.",
                "cta": "Hum se rabta karein",
                "sub": "Aap ke business ke liye banayi gayi ek website jo kaam karti hai."}


class BuilderAgent(BaseApartment):
    NAME = "builder"
    DESCRIPTION = "Builds sandboxed single-file demo websites for free-mockup offers"
    PERMISSIONS = {"internet": False, "email": False, "whatsapp": False,
                   "browser": False, "subprocess": False}

    MODEL = "layouts.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        learned = self.load_model(self.MODEL, default={}) or {}
        model = {
            "themes": CATEGORY_THEMES,
            "default_theme": DEFAULT_THEME,
            "copy": CATEGORY_COPY,
            "default_copy": DEFAULT_COPY,
            "built": int(learned.get("built", 0)),
            "feedback": learned.get("feedback", {"good": 0, "bad": 0}),
            "sections_order": learned.get("sections_order") or
            ["hero", "services", "proof", "contact", "footer"],
        }
        self.save_model(self.MODEL, model)
        self.log("trained", themes=len(CATEGORY_THEMES))
        return {"themes": len(CATEGORY_THEMES), "sections": len(model["sections_order"])}

    def theme_for(self, category: str) -> Dict[str, Any]:
        model = self.load_model(self.MODEL, default={}) or {}
        themes = model.get("themes") or CATEGORY_THEMES
        return themes.get(category, DEFAULT_THEME)

    # ------------------------------------------------------------------ build
    def build(self, lead: Dict[str, Any], brief: Optional[Dict[str, Any]] = None,
              pages: int = 1) -> Dict[str, Any]:
        """Generate the demo site.  Returns paths + a short summary."""
        self.permit("build_demo_site", {"lead": lead.get("name", ""), "pages": pages})
        category = lead.get("category") or "default"
        theme = self.theme_for(category)
        copy = CATEGORY_COPY.get(category, DEFAULT_COPY)
        answers = (brief or {}).get("answers", {}) if brief else {}
        phone = lead.get("phone") or ""
        whatsapp = re.sub(r"[^0-9]", "", phone)
        if whatsapp.startswith("0"):
            whatsapp = "92" + whatsapp[1:]
        email = lead.get("email") or ""
        name = _e(lead.get("name") or "Aap ka Business")
        city = _e(lead.get("city") or "")

        headline = _e(str(answers.get("headline") or copy["headline"]))
        sub = _e(str(answers.get("subheadline") or copy["sub"]))
        features = _features_for(category, answers)

        html_text = self._render_html(name=name, city=city, headline=headline, sub=sub,
                                      theme=theme, cta=_e(copy["cta"]), features=features,
                                      whatsapp=whatsapp, email=email, phone=_e(phone),
                                      category=_e(category.replace("_", " ")))

        folder = "demos/%s" % (lead.get("id") or "preview")
        self.mkdir(folder)
        index = self.write_text("%s/index.html" % folder, html_text)
        meta = {
            "lead_id": lead.get("id"), "business": lead.get("name"),
            "category": category, "generated_at": time.time(),
            "bytes": len(html_text.encode("utf-8")),
            "whatsapp": whatsapp, "pages": pages,
        }
        self.write_json("%s/meta.json" % folder, meta)
        self.db.put_item("demo:%s" % (lead.get("id") or "preview"), meta)

        model = self.load_model(self.MODEL, default={}) or {}
        model["built"] = int(model.get("built", 0)) + 1
        self.save_model(self.MODEL, model)

        if lead.get("id"):
            store = default_store()
            store.log_event("builder", "demo_built", {"lead_id": lead["id"],
                                                      "bytes": meta["bytes"]})
        self.log("built", lead=lead.get("name"), bytes=meta["bytes"])
        return {"path": str(index), "url_hint": "file:///%s" % index.as_posix(), **meta}

    def _render_html(self, **v) -> str:
        features_html = "\n".join(
            """      <div class="card">
        <h3>%s</h3>
        <p>%s</p>
      </div>""" % (_e(f["title"]), _e(f["text"])) for f in v["features"])
        whatsapp_button = ""
        if v["whatsapp"]:
            whatsapp_button = ('\n      <a class="btn" '
                               'href="https://wa.me/%s?text=Salam%%2C%%20mujhe%%20information%%20chahiye">'
                               '%s</a>' % (v["whatsapp"], v["cta"]))
        contact_line = " ".join(filter(None, [v["phone"], v["email"]]))
        return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(name)s%(city)s</title>
<meta name="description" content="%(headline)s - %(name)s%(city)s">
<style>
  :root { --primary: %(primary)s; --accent: %(accent)s; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         color:#111827; line-height:1.6; }
  header { background: var(--primary); color:#fff; padding: 3.5rem 1.25rem; text-align:center; }
  header h1 { margin:0 0 .5rem; font-size: clamp(1.6rem, 5vw, 2.6rem); }
  header p { margin:0 auto; max-width: 40rem; opacity:.92; }
  .btn { display:inline-block; margin-top:1.25rem; background: var(--accent); color:#111;
         padding:.85rem 1.6rem; border-radius:999px; font-weight:700; text-decoration:none; }
  main { max-width: 62rem; margin: 0 auto; padding: 2rem 1.25rem; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(15rem,1fr)); gap:1rem; }
  .card { border:1px solid #e5e7eb; border-radius:.9rem; padding:1.1rem; background:#fff; }
  .card h3 { margin:.2rem 0 .4rem; color: var(--primary); font-size:1.05rem; }
  .card p { margin:0; color:#4b5563; font-size:.95rem; }
  section { margin-bottom: 2rem; }
  h2 { font-size:1.4rem; margin:0 0 1rem; }
  .proof { background:#f9fafb; border-left:4px solid var(--accent); padding:1rem 1.25rem;
           border-radius:.5rem; }
  footer { background:#111827; color:#d1d5db; text-align:center; padding:2rem 1.25rem;
           font-size:.9rem; }
  .whatsapp-float { position:fixed; right:1rem; bottom:1rem; background:#25D366; color:#fff;
           border-radius:999px; padding:.8rem 1.2rem; font-weight:700; text-decoration:none;
           box-shadow:0 6px 18px rgba(0,0,0,.25); }
  @media (max-width: 480px) { header { padding: 2.5rem 1rem; } }
</style>
</head>
<body>
<header>
  <h1>%(headline)s</h1>
  <p>%(sub)s</p>
  %(whatsapp_button)s
</header>
<main>
  <section>
    <h2>Hum kya pesh karte hain</h2>
    <div class="grid">
%(features_html)s
    </div>
  </section>
  <section class="proof">
    <strong>%(name)s</strong> - %(city)s | %(category)s<br>
    Rabta: %(contact)s
  </section>
</main>
<footer>
  <div>%(name)s &middot; %(city)s</div>
  <div style="margin-top:.4rem;opacity:.7">Demo mockup - ClientHunter Factory se banaya gaya</div>
</footer>
%(float)s
</body>
</html>
""" % {
            "name": v["name"], "city": (" - " + v["city"]) if v["city"] else "",
            "headline": v["headline"], "sub": v["sub"],
            "primary": v["theme"]["primary"], "accent": v["theme"]["accent"],
            "whatsapp_button": whatsapp_button, "features_html": features_html,
            "category": v["category"], "contact": _e(contact_line) or "WhatsApp par rabta karein",
            "float": ('<a class="whatsapp-float" href="https://wa.me/%s">WhatsApp</a>'
                      % v["whatsapp"]) if v["whatsapp"] else "",
        }

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        text = (user_input or "").strip()
        lowered = text.lower()
        store = default_store()
        lead = None
        if context and context.get("lead_id"):
            lead = store.get_lead(int(context["lead_id"]))
        if lead is None:
            for candidate in store.leads(limit=300):
                name = str(candidate.get("name", "")).lower()
                if name and name in lowered:
                    lead = candidate
                    break
        if lead is None:
            import re as _re
            match = _re.search(r"(?:client_|lead_|#)(\d+)", lowered)
            if match:
                lead = store.get_lead(int(match.group(1)))
        if lead is None:
            leads = store.leads(limit=1)
            lead = leads[0] if leads else None
        if lead is None:
            return "Koi lead nahi mila. Pehle hunter chalayein."

        result = self.build(lead)
        return ("Demo site ready: %s\n\n%s\n\nFile: %s (%.1f KB)\nWhatsApp button: %s\n\n"
                "Client ko bhejne ke liye: apt_outreach ya apt_followup use karein."
                % (lead.get("name"),
                   "Ek single-file HTML - inline CSS, koi external dependency nahi, "
                   "mobile pe turant khulta hai.",
                   result["path"], result["bytes"] / 1024.0,
                   ("wa.me/%s" % result["whatsapp"]) if result["whatsapp"] else "nahi (no phone)"))

    def record_feedback(self, lead_id: int, good: bool) -> Dict[str, int]:
        model = self.load_model(self.MODEL, default={}) or {}
        feedback = model.get("feedback", {"good": 0, "bad": 0})
        feedback["good" if good else "bad"] = int(feedback.get("good" if good else "bad", 0)) + 1
        model["feedback"] = feedback
        self.save_model(self.MODEL, model)
        return feedback


def _e(value: Any) -> str:
    """Escape for safe HTML embedding - the only untrusted input here."""
    return html.escape(str(value or ""), quote=True)


def _features_for(category: str, answers: Dict[str, Any]) -> List[Dict[str, str]]:
    """Demo content per category (never invent prices or promises)."""
    library = {
        "restaurant": [("Menu", "Har dish ki photo aur rate - mobile pe asaani se"),
                       ("WhatsApp Order", "Ek tap mein order, koi app nahi"),
                       ("Location", "Google Map ke sath - raasta dhoondhna asaan")],
        "clinic": [("Appointment", "Online form - phone par wait karne ki zarurat nahi"),
                   ("Doctors", "Profile, timings aur specialization"),
                   ("Timings", "Rozana ki timings aur emergency contact")],
        "salon": [("Services", "Har service ka rate aur waqt"),
                  ("Bridal Packages", "Packages ki mukammal tafseel"),
                  ("Booking", "WhatsApp par slot confirm karein")],
        "gym": [("Programs", "Weight loss, strength, cardio plans"),
                ("Trainers", "Certified trainers ki profile"),
                ("Trial", "Free trial membership form")],
        "hotel": [("Rooms", "Har room ki photos, rate aur facilities"),
                  ("Direct Booking", "Koi commission nahi - behtar rate"),
                  ("Location", "Map aur nearby attractions")],
    }
    items = library.get(category)
    if not items:
        items = [("Services", "Aap ki services ki mukammal list"),
                 ("About", "Aap ka tajurba aur team"),
                 ("Contact", "Call, WhatsApp aur email - sab jagah")]
    return [{"title": title, "text": text} for title, text in items]
