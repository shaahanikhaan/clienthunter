"""apt_hunter - lead discovery.

Finds local Pakistani businesses and writes them into the shared leads table.

Three ways in, in order of preference:
1. Google Maps through the user's real Chrome (CDP) - Maps is JS-only, so a
   plain HTTP GET returns an empty shell.
2. DuckDuckGo + directory pages when Chrome is unavailable.
3. ``data/sample_leads.csv`` (or your own CSV) - the manual fallback that keeps
   the factory running when Google serves captchas.

It also learns: every city and category carries a conversion weight that
apt_self_trainer updates from real outcomes, so over time the hunter spends its
daily request budget where the money actually is.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from ...store import default_store
from ..base_apartment import BaseApartment


class HunterAgent(BaseApartment):
    NAME = "hunter"
    DESCRIPTION = "Finds local Pakistani businesses (Google Maps + directories + CSV import)"
    PERMISSIONS = {"internet": True, "email": False, "whatsapp": False,
                   "browser": True, "subprocess": False}

    MODEL = "city_score.json"

    # ------------------------------------------------------------------ train
    def train(self) -> Dict[str, Any]:
        cities = self.load_shared_data("pakistani_cities.json", default={}) or {}
        categories = self.load_shared_data("business_categories.json", default={}) or {}
        city_list = cities.get("cities", [])
        category_list = categories.get("categories", [])
        if not city_list or not category_list:
            raise ValueError("pakistani_cities.json / business_categories.json missing")

        learned = self.load_model(self.MODEL, default={}) or {}
        city_weights = learned.get("city_weights") or {}
        category_weights = learned.get("category_weights") or {}

        # Seed from the data files; never overwrite weights the trainer learned.
        for city in city_list:
            name = city["name"]
            if name not in city_weights:
                density = {"very_high": 1.4, "high": 1.2, "medium": 1.0, "low": 0.8}
                city_weights[name] = density.get(city.get("density", "medium"), 1.0)
        for category in category_list:
            cid = category["id"]
            if cid not in category_weights:
                category_weights[cid] = float(category.get("conversion_weight", 1.0))

        model = {
            "city_weights": city_weights,
            "category_weights": category_weights,
            "runs": int(learned.get("runs", 0)),
            "leads_found": int(learned.get("leads_found", 0)),
            "notes": "Weights are multipliers on lead priority. apt_self_trainer updates "
                     "them from won/lost outcomes - higher means hunt here first.",
        }
        self.save_model(self.MODEL, model)
        self.remember("train_summary", {"cities": len(city_list),
                                        "categories": len(category_list)})
        self.log("trained", cities=len(city_list), categories=len(category_list))
        return model

    # -------------------------------------------------------------- data views
    def cities(self) -> List[Dict[str, Any]]:
        data = self.load_shared_data("pakistani_cities.json", default={}) or {}
        return data.get("cities", [])

    def categories(self) -> List[Dict[str, Any]]:
        data = self.load_shared_data("business_categories.json", default={}) or {}
        return data.get("categories", [])

    def weights(self) -> Dict[str, Dict[str, float]]:
        model = self.load_model(self.MODEL, default={}) or {}
        return {"city": model.get("city_weights", {}),
                "category": model.get("category_weights", {})}

    def best_targets(self, count: int = 5) -> List[Dict[str, Any]]:
        """City/category pairs worth hunting today, by learned weight."""
        weights = self.weights()
        pairs = []
        for city, city_weight in weights["city"].items():
            for category, category_weight in weights["category"].items():
                pairs.append({"city": city, "category": category,
                              "weight": round(city_weight * category_weight, 3)})
        pairs.sort(key=lambda p: -p["weight"])
        return pairs[:count]

    # ------------------------------------------------------------------ parsing
    def parse_request(self, text: str) -> Dict[str, Any]:
        """Pull city + category out of a sentence like 'Karachi mein restaurants dhoondo'."""
        lowered = (text or "").lower()
        city, category, area = None, None, None

        for entry in self.cities():
            name = entry["name"]
            if name.lower() in lowered or (entry.get("urdu", "") in (text or "")):
                city = name
                for candidate in entry.get("areas", []):
                    if candidate.lower() in lowered:
                        area = candidate
                        break
                break

        for entry in self.categories():
            haystacks = [entry["id"].replace("_", " ")] + \
                        [str(k) for k in entry.get("keywords_roman", [])] + \
                        [str(k) for k in entry.get("keywords_urdu", [])]
            if any(token.lower() in lowered for token in haystacks):
                category = entry["id"]
                break

        limit_match = (re.search(r"(\d{1,3})\s*(?:leads?|clients?|business)", lowered)
                       or re.search(r"(\d{1,3})\s*$", lowered)      # "dhoondo 8"
                       or re.search(r"^(\d{1,3})\b", lowered))      # "8 leads"
        limit = int(limit_match.group(1)) if limit_match else 20
        return {"city": city, "category": category, "area": area,
                "limit": max(1, min(limit, 60))}

    # ----------------------------------------------------------------- hunting
    def find_leads(self, city: str, category: str, limit: int = 20,
                   area: Optional[str] = None,
                   progress=None) -> List[Dict[str, Any]]:
        """Scrape and store.  Returns the leads that were actually saved."""
        self.require("internet")
        self.permit("scrape_maps", {"city": city, "category": category})
        scrape = self.tool("scrape")
        max_leads = int(self.config.get("limits", {}).get("max_leads_per_run", 40))
        limit = min(limit, max_leads)

        try:
            raw = scrape.scrape_google_maps(city, category, limit=limit, area=area,
                                            progress=progress)
        except Exception as exc:                 # PermissionDenied / no network / no Chrome
            self.log("maps_scrape_failed", error="%s: %s" % (type(exc).__name__, exc)[:160])
            raw = []
        if not raw:
            raw = self._import_fallback(city, category, limit)
        return self.save_leads(raw)

    # -------------------------------------------------- area sweep + resume
    def hunt_city(self, city: str, category: str, limit: int = 30,
                  areas: Optional[List[str]] = None,
                  progress=None) -> List[Dict[str, Any]]:
        """Sweep a city neighbourhood by neighbourhood, resumably.

        A single Maps query only ever exposes the first page of results, so
        serious scrapers walk a grid.  Here the grid is the city's own area
        list from ``data/pakistani_cities.json``.  Progress is checkpointed, so
        an interrupted run (captcha, network drop, Ctrl+Shift+Q) resumes from
        the next neighbourhood instead of starting over.
        """
        scrape = self.tool("scrape")
        pending = list(areas or scrape.city_areas(city)) or [""]
        checkpoint = self._checkpoint()
        key = "%s|%s" % (city.lower(), category.lower())
        done = set(checkpoint.get(key, {}).get("areas", []))
        saved: List[Dict[str, Any]] = []
        for area in pending:
            if len(saved) >= limit or self.kill_switch():
                break
            if area in done:
                continue
            if progress:
                progress("area: %s" % (area or city))
            batch = self.find_leads(city, category, max(1, limit - len(saved)),
                                    area=area or None, progress=progress)
            saved.extend(batch)
            done.add(area)
            checkpoint[key] = {"areas": sorted(done), "at": time.time()}
            self._save_checkpoint(checkpoint)
        if len(done) >= len(pending):
            checkpoint.pop(key, None)
            self._save_checkpoint(checkpoint)
        return saved

    def _checkpoint(self) -> Dict[str, Any]:
        return self.read_json("checkpoint.json", default={}) or {}

    def _save_checkpoint(self, data: Dict[str, Any]) -> None:
        self.write_json("checkpoint.json", data)

    def refresh_stale(self, days: int = 30, limit: int = 20) -> List[Dict[str, Any]]:
        """Re-check leads older than ``days`` - Google ToS §3.2.3 caps caching.

        Leads whose stored ``scraped_at`` is past the window are re-fetched by
        place id and their details (phone/website/rating) refreshed.  Returns
        the leads that changed.
        """
        cutoff = time.time() - float(days) * 86400
        store = default_store()
        stale = []
        for lead in store.leads(limit=500):
            notes = str(lead.get("notes") or "")
            stamp = 0.0
            match = re.search(r"scraped_at=([0-9.]+)", notes)
            if match:
                try:
                    stamp = float(match.group(1))
                except ValueError:
                    stamp = 0.0
            if stamp and stamp < cutoff:
                stale.append(lead)
        refreshed = []
        try:
            maps = self.tool("maps_api")
        except Exception:
            maps = None
        for lead in stale[:limit]:
            place_id = ""
            match = re.search(r"place_id=([0-9x:a-fA-F]+)", str(lead.get("notes") or ""))
            if match:
                place_id = match.group(1)
            detail = None
            if maps and place_id:
                detail = maps.place(place_id, float(lead.get("lat", 0) or 0),
                                    float(lead.get("lng", 0) or 0), lead.get("name", ""))
            if not detail:
                continue
            fields = {"phone": detail.get("phone") or lead.get("phone", ""),
                      "website": detail.get("website") or lead.get("website", ""),
                      "rating": float(detail.get("rating") or lead.get("rating") or 0),
                      "reviews": int(detail.get("reviews") or lead.get("reviews") or 0),
                      "notes": re.sub(r"scraped_at=[0-9.]+", "scraped_at=%.0f" % time.time(),
                                      str(lead.get("notes") or "")) or
                      ("scraped_at=%.0f" % time.time())}
            store.update_fields(int(lead["id"]), **fields)
            refreshed.append({"lead_id": lead["id"], "name": lead.get("name", ""),
                              "changed": True})
        self.log("refreshed_stale", checked=len(stale), refreshed=len(refreshed))
        return refreshed

    def _import_fallback(self, city: str, category: str,
                         limit: int) -> List[Dict[str, Any]]:
        """Offline / blocked: pull from the bundled CSV instead of doing nothing."""
        path = self.shared_data_path("sample_leads.csv")
        if not path.exists():
            return []
        rows = self._read_csv(path, city_default=city, category_default=category)
        for row in rows:
            row.setdefault("source", "sample_csv_offline_fallback")
        exact = [row for row in rows
                 if (not city or row.get("city", "").lower() == city.lower())
                 and (not category or row.get("category", "") == category)]
        # never pad the result with unrelated rows - an empty answer is honest,
        # a Karachi restaurant returned for "Islamabad hotels" is not.
        if exact:
            self.log("csv_fallback", city=city, category=category, rows=len(exact))
            return exact[:limit]
        same_city = [row for row in rows
                     if city and row.get("city", "").lower() == city.lower()]
        if same_city:
            self.log("csv_fallback_partial", city=city, category=category,
                     rows=len(same_city), note="category not in sample CSV")
            return same_city[:limit]
        self.log("csv_fallback_empty", city=city, category=category)
        return []

    @staticmethod
    def _read_csv(path, city_default: str = "", category_default: str = ""):
        """Read the bundled CSV straight off disk.

        The sandboxed file tool refuses absolute paths outside an apartment's
        sandbox, and ``factory/data/`` is shared by design - so shared corpora
        are read here instead.
        """
        import csv

        rows = []
        with open(path, "r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                clean = {key.strip(): (value or "").strip()
                         for key, value in row.items() if key}
                clean.setdefault("city", city_default)
                clean.setdefault("category", category_default)
                for numeric in ("rating", "reviews", "score"):
                    if numeric in clean:
                        try:
                            clean[numeric] = float(clean[numeric]) if numeric == "rating" \
                                else int(float(clean[numeric]))
                        except (TypeError, ValueError):
                            clean[numeric] = 0
                rows.append(clean)
        return rows

    def save_leads(self, leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Upsert into the shared DB and score each lead."""
        store = default_store()
        weights = self.weights()
        saved = []
        for lead in leads:
            if not lead.get("name"):
                continue
            city_weight = weights["city"].get(lead.get("city", ""), 1.0)
            category_weight = weights["category"].get(lead.get("category", ""), 1.0)
            rating = float(lead.get("rating") or 0)
            reviews = int(lead.get("reviews") or 0)
            score = int(round(20 * city_weight * category_weight
                              + min(reviews, 300) / 12
                              + (rating - 3.0) * 6))
            record = {
                "name": lead.get("name", "")[:90],
                "phone": lead.get("phone", ""),
                "email": lead.get("email", ""),
                "address": lead.get("address", "")[:160],
                "category": lead.get("category", ""),
                "city": lead.get("city", ""),
                "website": lead.get("website", ""),
                "rating": rating,
                "reviews": reviews,
                "stage": "new",
                "score": max(0, min(100, score)),
                "source": lead.get("source", "hunter"),
            }
            if lead.get("place_id") or lead.get("scraped_at"):
                stamp = "scraped_at=%.0f" % float(lead.get("scraped_at") or time.time())
                extras = ["place_id=%s" % lead["place_id"]] if lead.get("place_id") else []
                record["notes"] = " ".join([stamp] + extras)
            lead_id = store.upsert_lead(record)
            record["id"] = lead_id
            saved.append(record)
        if saved:
            store.log_event("hunter", "leads_found", {"count": len(saved)})
            self._bump_stats(len(saved))
        return saved

    def _bump_stats(self, count: int) -> None:
        model = self.load_model(self.MODEL, default={}) or {}
        model["runs"] = int(model.get("runs", 0)) + 1
        model["leads_found"] = int(model.get("leads_found", 0)) + count
        self.save_model(self.MODEL, model)

    def record_outcome(self, city: str, category: str, converted: bool) -> Dict[str, float]:
        """Called by apt_self_trainer: nudge the learned weights."""
        model = self.load_model(self.MODEL, default={}) or {}
        city_weights = model.get("city_weights", {})
        category_weights = model.get("category_weights", {})
        for table, key in ((city_weights, city), (category_weights, category)):
            if key in table:
                current = float(table[key])
                target = current + (0.15 if converted else -0.08)
                table[key] = round(max(0.4, min(2.0, current + 0.25 * (target - current))), 3)
        model["city_weights"] = city_weights
        model["category_weights"] = category_weights
        self.save_model(self.MODEL, model)
        return {"city": city_weights.get(city, 1.0), "category": category_weights.get(category, 1.0)}

    # ----------------------------------------------------------------- handle
    def handle(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        request = self.parse_request(user_input or "")
        city = request["city"]
        category = request["category"]
        if not city and not category:
            return self._help()
        if not city:
            city = (self.load_shared_data("pakistani_cities.json", default={}) or {}
                    ).get("default_city", "Karachi")
        if not category:
            best = self.best_targets(1)
            category = best[0]["category"] if best else "restaurant"

        if self.offline or not self.can("internet"):
            leads = self._import_fallback(city, category, request["limit"])
            saved = self.save_leads(leads)
            return ("Offline mode: Google Maps scrape skip kiya.\n%s\n\n"
                    "Internet permission on kar ke dobara chalaayein to live leads aayengi."
                    % self._summarise(saved))

        lowered = (user_input or "").lower()
        if any(word in lowered for word in ("refresh", "purane", "stale", "update karo")):
            refreshed = self.refresh_stale(limit=request["limit"])
            if not refreshed:
                return ("Koi lead 30 din se purana nahi hai (ya place_id missing hai). "
                        "Naye leads ke liye pehle hunt karein.")
            return "%d purane leads dobara check ho gaye:" % len(refreshed) + "\n" + \
                "\n".join("  #%s %s" % (row["lead_id"], row["name"][:50]) for row in refreshed)

        progress = context.get("progress") if context else None
        if request.get("area") or any(word in lowered for word in
                                      ("poora shehar", "full city", "sare areas", "grid")):
            saved = self.hunt_city(city, category, request["limit"],
                                   progress=progress)
        else:
            saved = self.find_leads(city, category, request["limit"],
                                    request["area"], progress)
        if not saved:
            return ("Koi naya lead nahi mila (%s / %s). Ho sakta hai Google ne captcha "
                    "de diya ho - CSV import ka rasta try karein: "
                    "data/sample_leads.csv" % (city, category))
        return self._summarise(saved)

    def _summarise(self, leads: List[Dict[str, Any]]) -> str:
        lines = ["%d leads mile (DB mein save ho gaye):" % len(leads)]
        for lead in leads[:10]:
            bits = [lead.get("name", "?")]
            if lead.get("city"):
                bits.append(lead["city"])
            if lead.get("phone"):
                bits.append(lead["phone"])
            if lead.get("rating"):
                bits.append("%.1f★ (%d)" % (lead["rating"], lead.get("reviews", 0)))
            if lead.get("website"):
                bits.append("site: hai")
            lines.append("  - " + " | ".join(bits))
        if len(leads) > 10:
            lines.append("  ...aur %d" % (len(leads) - 10))
        return "\n".join(lines)

    def _help(self) -> str:
        pairs = self.best_targets(5)
        lines = ["Kahan dhoondna hai? Format: '<city> mein <category> dhoondo'.",
                 "Sab se achay targets (learned weights ke hisaab se):"]
        for pair in pairs:
            lines.append("  - %s / %s  (weight %.2f)" % (pair["city"], pair["category"],
                                                         pair["weight"]))
        return "\n".join(lines)

    # ------------------------------------------------------------ daily cycle
    def run(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Autonomous entry point for the orchestrator's daily cycle."""
        all_leads: List[Dict[str, Any]] = []
        for target in self.best_targets(3):
            if self.kill_switch():
                break
            try:
                all_leads.extend(self.find_leads(target["city"], target["category"],
                                                 limit=max(limit // 3, 5)))
            except Exception as exc:
                self.log("run_failed", error=str(exc))
        return all_leads
