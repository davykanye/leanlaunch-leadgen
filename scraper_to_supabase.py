"""
scraper_to_supabase.py

Same two-stage scrape as the standalone version (Overpass sourcing +
Playwright/Google Maps verification for no-website, few-review leads),
but writes results directly into your Supabase `leads` table instead of
a local CSV. Built to run in GitHub Actions (see workflow file), so it
uses Supabase itself as the "already checked" tracker -- no local state
needed between runs on ephemeral CI machines.

Required environment variables (set as GitHub Actions secrets):
    SUPABASE_URL               e.g. https://xxxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY  service role key (server-side only, never
                                exposed to the frontend)

Setup:
    pip install requests playwright
    playwright install chromium
"""

import requests
import re
import time
import random
import os
import sys

# ---------------- CONFIG ----------------
CITIES = [
    ("Denver, CO",          39.7392, -104.9903, 30000),
    ("Aurora, CO",          39.7294, -104.8319, 20000),
    ("Colorado Springs, CO", 38.8339, -104.8214, 25000),
    ("Fort Collins, CO",    40.5853, -105.0844, 20000),
    ("Boulder, CO",         40.0150, -105.2705, 15000),
    ("Pueblo, CO",          38.2544, -104.6091, 15000),
]

NICHE_QUERIES = {
    "hvac":        ['["shop"="hvac"]', '["craft"="hvac"]'],
    "plumbing":    ['["shop"="plumber"]', '["craft"="plumber"]'],
    "roofing":     ['["craft"="roofer"]'],
    "electrician": ['["shop"="electrical"]', '["craft"="electrician"]'],
}
NICHES_TO_RUN = ["hvac", "plumbing", "roofing", "electrician"]

TARGET_TIER_A_COUNT = 500   # stop once this many Tier A (no site, few reviews) leads exist total
MAX_REVIEWS = 15
MIN_REVIEWS = 0
DELAY_SECONDS = 3
MAX_CANDIDATES_PER_RUN = 800  # safety cap so one Actions run can't blow past the time limit

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# -----------------------------------------


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_existing_osm_ids():
    """Pulls every osm_id already in Supabase so Stage 2 can skip re-checking them."""
    ids = set()
    offset = 0
    page_size = 1000
    while True:
        headers = {**supabase_headers(), "Range": f"{offset}-{offset + page_size - 1}"}
        resp = requests.get(f"{SUPABASE_URL}/rest/v1/leads?select=osm_id", headers=headers, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        ids.update(row["osm_id"] for row in page if row.get("osm_id"))
        if len(page) < page_size:
            break
        offset += page_size
    return ids


def count_tier_a():
    headers = {**supabase_headers(), "Prefer": "count=exact"}
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/leads?tier=eq.A&select=id", headers=headers, timeout=30)
    resp.raise_for_status()
    content_range = resp.headers.get("Content-Range", "*/0")
    return int(content_range.split("/")[-1])


def upsert_leads(rows):
    if not rows:
        return
    headers = {**supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    resp = requests.post(f"{SUPABASE_URL}/rest/v1/leads?on_conflict=osm_id",
                          headers=headers, json=rows, timeout=30)
    if resp.status_code >= 300:
        print(f"  WARNING: Supabase upsert failed ({resp.status_code}): {resp.text[:300]}")


# ---------- STAGE 1: Overpass sourcing ----------

def build_query(lat, lon, radius, tag_filters):
    filters = "".join(f'node{tf}(around:{radius},{lat},{lon});' for tf in tag_filters)
    filters += "".join(f'way{tf}(around:{radius},{lat},{lon});' for tf in tag_filters)
    return f"[out:json][timeout:60];({filters});out center tags;"


def fetch_city_niche(city_name, lat, lon, radius, niche):
    query = build_query(lat, lon, radius, NICHE_QUERIES[niche])
    resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=90)
    resp.raise_for_status()
    rows = []
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        elat = el.get("lat") or el.get("center", {}).get("lat")
        elon = el.get("lon") or el.get("center", {}).get("lon")
        addr_parts = [tags.get("addr:housenumber", ""), tags.get("addr:street", ""),
                      tags.get("addr:city", "") or city_name.split(",")[0],
                      tags.get("addr:state", "CO"), tags.get("addr:postcode", "")]
        rows.append({
            "osm_id": f"{niche}_{el.get('id')}",
            "name": name,
            "address": " ".join(p for p in addr_parts if p).strip(),
            "osm_website": tags.get("website") or tags.get("contact:website", ""),
            "niche": niche,
            "city": city_name,
            "lat": elat,
            "lon": elon,
        })
    return rows


def gather_raw_candidates():
    all_rows = []
    for city_name, lat, lon, radius in CITIES:
        for niche in NICHES_TO_RUN:
            print(f"Querying Overpass: {niche} near {city_name}...")
            try:
                rows = fetch_city_niche(city_name, lat, lon, radius, niche)
                all_rows.extend(rows)
                print(f"  -> {len(rows)} raw results")
            except Exception as e:
                print(f"  -> FAILED ({e}), skipping")
            time.sleep(1.5)

    seen, unique = set(), []
    for r in all_rows:
        key = (r["name"].strip().lower(), round(r["lat"] or 0, 4), round(r["lon"] or 0, 4))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ---------- STAGE 2: Google Maps verification ----------

def parse_rating_and_reviews(text):
    rating_match = re.search(r"(\d\.\d)", text)
    reviews_match = re.search(r"\((\d[\d,]*)\)", text)
    rating = float(rating_match.group(1)) if rating_match else None
    reviews = int(reviews_match.group(1).replace(",", "")) if reviews_match else None
    return rating, reviews


def check_candidate(page, candidate):
    query = f"{candidate['name']} {candidate['address']}".strip()
    page.goto(f"https://www.google.com/maps/search/{query}", timeout=20000)
    page.wait_for_timeout(2200)

    out = {"gmaps_rating": None, "gmaps_reviews": None, "has_website": None, "gmaps_phone": ""}
    try:
        panel = page.locator("div[role='main']").first
        panel.wait_for(timeout=5000)
        panel_text = panel.inner_text()
        if candidate["name"].lower()[:10] not in panel_text.lower():
            return None

        rating, reviews = parse_rating_and_reviews(panel_text)
        out["gmaps_rating"] = rating
        out["gmaps_reviews"] = reviews

        site_link = page.locator("a[data-item-id='authority']").first
        out["has_website"] = site_link.count() > 0

        phone_match = re.search(r"\(\d{3}\)\s?\d{3}-\d{4}|\d{3}[-.\s]\d{3}[-.\s]\d{4}", panel_text)
        if phone_match:
            out["gmaps_phone"] = phone_match.group(0)
    except Exception:
        return None
    return out


def score_tier(has_website, reviews):
    if reviews is None:
        return "C"
    in_range = MIN_REVIEWS <= reviews <= MAX_REVIEWS
    if in_range and not has_website:
        return "A"
    if in_range and has_website:
        return "B"
    return "C"


def make_icebreaker(name, has_website, rating, reviews):
    if not has_website and rating and reviews:
        return (f"Noticed {name} has a {rating}-star rating from {reviews} reviews "
                f"but no website showing up -- likely losing jobs to competitors "
                f"who show up first in local search.")
    if rating and reviews:
        return (f"Saw {name}'s {rating}-star rating ({reviews} reviews) -- curious if "
                f"the current site is converting that trust into booked jobs.")
    return f"Came across {name} while researching local service businesses."


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set as environment variables.")
        sys.exit(1)

    existing_tier_a = count_tier_a()
    print(f"Currently {existing_tier_a}/{TARGET_TIER_A_COUNT} Tier A leads in Supabase.")
    if existing_tier_a >= TARGET_TIER_A_COUNT:
        print("Target already met. Nothing to do this run.")
        return

    candidates = gather_raw_candidates()
    print(f"\n{len(candidates)} unique raw candidates from Overpass.")

    already_checked = fetch_existing_osm_ids()
    remaining = [c for c in candidates if c["osm_id"] not in already_checked]
    random.shuffle(remaining)
    remaining = remaining[:MAX_CANDIDATES_PER_RUN]
    print(f"{len(remaining)} unchecked candidates to process this run "
          f"(capped at {MAX_CANDIDATES_PER_RUN} per run).\n")

    batch = []
    tier_a_found = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for i, candidate in enumerate(remaining, 1):
            result = check_candidate(page, candidate)

            if result is not None:
                tier = score_tier(result["has_website"], result["gmaps_reviews"])
                row = {
                    "osm_id": candidate["osm_id"],
                    "name": candidate["name"],
                    "address": candidate["address"],
                    "niche": candidate["niche"],
                    "city": candidate["city"],
                    "gmaps_rating": result["gmaps_rating"],
                    "gmaps_reviews": result["gmaps_reviews"],
                    "gmaps_phone": result["gmaps_phone"],
                    "lat": candidate["lat"],
                    "lon": candidate["lon"],
                    "tier": tier,
                    "icebreaker": make_icebreaker(candidate["name"], result["has_website"],
                                                   result["gmaps_rating"], result["gmaps_reviews"]),
                }
                batch.append(row)
                if tier == "A":
                    tier_a_found += 1
                    print(f"  MATCH ({existing_tier_a + tier_a_found}/{TARGET_TIER_A_COUNT}): "
                          f"{candidate['name']} -- {result['gmaps_reviews']} reviews, no website")

            if len(batch) >= 25:
                upsert_leads(batch)
                batch = []

            if i % 50 == 0:
                print(f"  ...processed {i}/{len(remaining)}")

            if existing_tier_a + tier_a_found >= TARGET_TIER_A_COUNT:
                print(f"\nTarget of {TARGET_TIER_A_COUNT} Tier A leads reached.")
                break

            time.sleep(DELAY_SECONDS)

        browser.close()

    upsert_leads(batch)
    print(f"\nRun complete. {tier_a_found} new Tier A leads added this run "
          f"({existing_tier_a + tier_a_found} total).")


if __name__ == "__main__":
    from playwright.sync_api import sync_playwright
    main()
