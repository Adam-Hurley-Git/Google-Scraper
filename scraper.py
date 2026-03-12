"""
Google Local Business Scraper

Scrapes business information from Google Search local/business results using
Playwright with stealth mode. Extracts: name, address, phone, website, rating,
reviews count, hours, category, and more.

Usage:
    python scraper.py --query "restaurants" --location "Swansea UK" --max-results 50
    python scraper.py --url "https://www.google.com/search?q=restaurant&udm=1&..."
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import random
import shutil
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs

from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------

def find_chromium_executable() -> str | None:
    """Locate a usable Chromium/Chrome binary for Playwright."""
    import pathlib

    # 1. Check Playwright cache for any installed chromium
    pw_cache = pathlib.Path.home() / ".cache" / "ms-playwright"
    if pw_cache.exists():
        for d in sorted(pw_cache.iterdir(), reverse=True):
            if "chromium" in d.name and "headless_shell" not in d.name:
                candidate = d / "chrome-linux" / "chrome"
                if candidate.exists():
                    return str(candidate)

    # 2. Fallback: system Chrome / Chromium
    for name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
        path = shutil.which(name)
        if path:
            return path

    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def extract_phone(text: str) -> str | None:
    """Try to pull a phone number from a text blob."""
    patterns = [
        r'(\+?\d[\d\s\-().]{7,}\d)',
        r'(\d{3}[\s.-]\d{3}[\s.-]\d{4})',
        r'(\(\d{3}\)\s?\d{3}[\s.-]\d{4})',
        r'(\d{4}\s?\d{3}\s?\d{4})',
        r'(\d{5}\s?\d{6})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return None


def build_search_url(query: str, location: str | None = None) -> str:
    """Build a Google local-results URL (udm=1 triggers local/places mode)."""
    q = query
    if location:
        q = f"{query} in {location}"
    params = {"q": q, "udm": "1"}
    return f"https://www.google.com/search?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Core scraper helpers
# ---------------------------------------------------------------------------

# Canonical selector for business cards — data-cid is a stable Google
# attribute (unique per business) that avoids overlapping-selector dupes.
CARD_SELECTOR = "div[data-cid]"

SOCIAL_DOMAINS = [
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "linkedin.com", "youtube.com", "yelp.com",
]


def collect_cids_from_page(page) -> list[str]:
    """Return de-duped list of data-cid values from all business cards on current page."""
    cards = page.query_selector_all(CARD_SELECTOR)
    cids: list[str] = []
    seen: set[str] = set()
    for card in cards:
        cid = card.get_attribute("data-cid")
        if cid and cid not in seen:
            cids.append(cid)
            seen.add(cid)
    return cids


def merge_detail(info: dict, extra: dict):
    """Merge detail panel data into listing info, preferring detail data."""
    if extra.get("website"):
        info["website"] = extra["website"]
    if extra.get("phone"):
        info["phone"] = extra["phone"]
    if extra.get("address"):
        info["address"] = extra["address"]
    if extra.get("socials"):
        info["socials"] = extra["socials"]


# ---------------------------------------------------------------------------
# Listing card extraction
# ---------------------------------------------------------------------------

def scrape_business_panel(page, element) -> dict:
    """Extract all available data from a single business listing element."""
    info = {
        "name": None,
        "address": None,
        "phone": None,
        "website": None,
        "rating": None,
        "reviews_count": None,
        "category": None,
        "hours": None,
        "price_range": None,
        "description": None,
        "services": [],
        "google_maps_url": None,
    }

    # --- Name ---
    for sel in [
        "div[role='heading']",
        "[data-attrid='title'] span",
        ".dbg0pd",
        ".OSrXXb",
        "span.OSrXXb",
        "a .VkpGBb",
    ]:
        el = element.query_selector(sel)
        if el:
            txt = el.inner_text().strip()
            if txt:
                info["name"] = txt
                break

    # --- Link / website ---
    for sel in [
        "a[data-attrid='visit_website']",
        "a[ping][href]:not([href*='google'])",
        "a.yYlJEf",
    ]:
        el = element.query_selector(sel)
        if el:
            href = el.get_attribute("href") or ""
            if href and "google" not in href:
                info["website"] = href
                break

    # --- Google Maps link ---
    maps_link = element.query_selector("a[href*='maps.google'], a[href*='google.com/maps']")
    if maps_link:
        info["google_maps_url"] = maps_link.get_attribute("href")

    # --- Rating (prefer aria-label based, then class-based) ---
    for sel in [
        "span[aria-label*='Rated']",
        "span[aria-label*='star']",
        "span.yi40Hd",
        "span.Fam1ne",
    ]:
        el = element.query_selector(sel)
        if el:
            label = el.get_attribute("aria-label") or el.inner_text()
            m = re.search(r'([\d.]+)', label)
            if m:
                info["rating"] = m.group(1)
                break

    # --- Reviews count (prefer aria-label, then class-based) ---
    rating_el = element.query_selector("span[aria-label*='review']")
    if rating_el:
        label = rating_el.get_attribute("aria-label") or ""
        m = re.search(r'([\d,]+)\s*review', label)
        if m:
            info["reviews_count"] = m.group(1).replace(",", "")
    if not info["reviews_count"]:
        for sel in ["span.RDApEe", "span.hqzQac"]:
            el = element.query_selector(sel)
            if el:
                txt = el.inner_text()
                m = re.search(r'([\d,]+)', txt)
                if m:
                    info["reviews_count"] = m.group(1).replace(",", "")
                    break

    # --- Grab all visible text and parse phone / address from it ---
    full_text = element.inner_text()
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    if not info["phone"]:
        info["phone"] = extract_phone(full_text)

    # --- Category ---
    for sel in [".YhemCb", ".rllt__wrapped"]:
        el = element.query_selector(sel)
        if el:
            info["category"] = el.inner_text().strip()
            break
    # Heuristic: short line right after name that isn't the address or phone
    if not info["category"] and len(lines) > 1:
        candidate = lines[1] if info["name"] and lines[0] == info["name"] else None
        if candidate and len(candidate) < 40 and not extract_phone(candidate):
            info["category"] = candidate

    # --- Address (heuristic: line with a number that looks like a street address) ---
    for line in lines:
        if re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Blvd|Dr|Ln|Way|Street|Road|Avenue|Place|Pl|Ct|Sq|High|Terr)', line, re.I):
            info["address"] = line
            break
    # Fallback: look for lines with postcodes
    if not info["address"]:
        for line in lines:
            if re.search(r'\b[A-Z]{1,2}\d[\dA-Z]?\s?\d[A-Z]{2}\b', line):  # UK postcode
                info["address"] = line
                break
            if re.search(r'\b\d{5}(-\d{4})?\b', line):  # US zip
                info["address"] = line
                break

    # --- Hours ---
    for sel in [".rllt__details div:has-text('Open')", "span:has-text('Open')", "span:has-text('Closed')"]:
        try:
            el = element.query_selector(sel)
            if el:
                info["hours"] = el.inner_text().strip()
                break
        except Exception:
            pass
    if not info["hours"]:
        for line in lines:
            if re.search(r'(Open|Closed|Hours|am|pm|AM|PM)', line):
                info["hours"] = line
                break

    # --- Price range ---
    m = re.search(r'([$£€]{1,4})\s*[-–]\s*([$£€]{1,4})', full_text)
    if m:
        info["price_range"] = f"{m.group(1)}-{m.group(2)}"
    else:
        m = re.search(r'([$£€]{1,4})(?:\s|$)', full_text)
        if m and len(m.group(1)) <= 4:
            info["price_range"] = m.group(1)

    # --- Services (dine-in, takeaway, delivery) ---
    for kw in ["Dine-in", "Takeaway", "Takeout", "Delivery", "Kerbside pickup", "Curbside pickup", "In-store pickup"]:
        if kw.lower() in full_text.lower():
            info["services"].append(kw)

    return info


# ---------------------------------------------------------------------------
# Detail panel extraction (click-based, NOT page navigation)
# ---------------------------------------------------------------------------

def scrape_detail_panel(page, card, timeout: int = 8000) -> dict:
    """Click a business card to open its detail panel, extract data, then close it."""
    extra = {
        "website": None,
        "phone": None,
        "address": None,
        "socials": [],
    }

    url_before = page.url

    try:
        # 1. Click the card to open the detail panel
        card.click()
        page.wait_for_timeout(random.randint(1500, 2500))

        # 2. Check if it navigated away (some cards are links)
        navigated = page.url != url_before

        # 3. Wait for detail panel content to appear
        try:
            page.wait_for_selector(
                "[data-attrid*='phone'], [data-attrid*='address'], "
                "a[data-attrid='visit_website'], a[href^='tel:']",
                timeout=timeout,
            )
        except Exception:
            pass  # Panel may not have all fields; extract what we can

        page.wait_for_timeout(random.randint(500, 1000))

        # 4. Extract website
        for sel in [
            "a[data-attrid='visit_website']",
            "[data-attrid='visit_website'] a[href]",
            "a[href]:has-text('Website')",
            "a.n1obkb[href]",
        ]:
            website_el = page.query_selector(sel)
            if website_el:
                href = website_el.get_attribute("href") or ""
                if href and "google" not in href:
                    extra["website"] = href
                    break

        # 5. Extract phone
        for sel in [
            "a[href^='tel:']",
            "[data-attrid*='phone'] span.LrzXr",
            "[data-attrid*='phone'] span",
        ]:
            phone_el = page.query_selector(sel)
            if phone_el:
                if sel.startswith("a[href"):
                    href = phone_el.get_attribute("href") or ""
                    txt = href.replace("tel:", "").strip()
                else:
                    txt = phone_el.inner_text().strip()
                if txt and len(txt) > 5:
                    extra["phone"] = txt
                    break

        # 6. Extract address
        for sel in [
            "[data-attrid*='address'] span.LrzXr",
            "[data-attrid*='address'] span",
        ]:
            addr_el = page.query_selector(sel)
            if addr_el:
                txt = addr_el.inner_text().strip()
                if txt:
                    extra["address"] = txt
                    break

        # 7. Extract social links
        seen_socials: set[str] = set()
        for link in page.query_selector_all(
            "[data-attrid*='social'] a[href], "
            "a[href*='facebook.com'], a[href*='instagram.com'], "
            "a[href*='twitter.com'], a[href*='x.com'], "
            "a[href*='tiktok.com'], a[href*='linkedin.com'], "
            "a[href*='youtube.com'], a[href*='yelp.com']"
        ):
            href = link.get_attribute("href") or ""
            for domain in SOCIAL_DOMAINS:
                if domain in href and href not in seen_socials:
                    extra["socials"].append(href)
                    seen_socials.add(href)
                    break

        # 8. Close the panel / go back
        if navigated:
            page.go_back(wait_until="domcontentloaded", timeout=10_000)
            page.wait_for_timeout(random.randint(1000, 2000))
        else:
            # Try Escape to close the side panel
            page.keyboard.press("Escape")
            page.wait_for_timeout(random.randint(800, 1500))
            # If panel is still open (URL has a fragment or panel element), try back button
            close_btn = page.query_selector("button[aria-label='Close'], g-back-button")
            if close_btn:
                close_btn.click()
                page.wait_for_timeout(random.randint(500, 1000))

    except Exception as e:
        print(f" [!] detail error: {e}")
        # Recover: try Escape and go_back as fallbacks
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            if page.url != url_before:
                page.go_back(wait_until="domcontentloaded", timeout=5000)
                page.wait_for_timeout(1000)
        except Exception:
            pass

    return extra


# ---------------------------------------------------------------------------
# Page-level helpers
# ---------------------------------------------------------------------------

def handle_consent(page):
    """Dismiss Google's cookie consent / GDPR banner if present."""
    try:
        accept_btn = page.query_selector(
            "button:has-text('Accept all'), button:has-text('Accept'), "
            "button:has-text('I agree'), button:has-text('Reject all')"
        )
        if accept_btn:
            accept_btn.click()
            page.wait_for_timeout(1500)
    except Exception:
        pass


def scrape_page_of_results(
    page, seen_cids: set, results: list, max_results: int, detail_scrape: bool,
):
    """Scrape all business cards on the current page, appending to results.

    Uses CID-first strategy: collect all data-cid values, then re-query each
    card individually. This avoids stale element references after detail panel
    interactions.
    """
    cids = collect_cids_from_page(page)
    new_on_page = 0

    for cid in cids:
        if len(results) >= max_results:
            break
        if cid in seen_cids:
            continue
        seen_cids.add(cid)

        # Re-query the card by CID (safe against stale references)
        card = page.query_selector(f'{CARD_SELECTOR}[data-cid="{cid}"]')
        if not card:
            continue

        count = len(results) + 1
        print(f"  [{count}] Scraping...", end="")

        try:
            info = scrape_business_panel(page, card)
            if not info["name"]:
                print(" skipped (no name)")
                continue

            # Detail panel scraping (click card → extract → close)
            if detail_scrape:
                # Re-query card again in case DOM shifted
                card = page.query_selector(f'{CARD_SELECTOR}[data-cid="{cid}"]')
                if card:
                    extra = scrape_detail_panel(page, card)
                    merge_detail(info, extra)

            # Convert lists to strings for CSV
            if isinstance(info.get("socials"), list):
                info["socials"] = "; ".join(info["socials"]) if info["socials"] else ""
            if isinstance(info.get("services"), list):
                info["services"] = "; ".join(info["services"]) if info["services"] else ""

            print(f" {info['name']}")
            results.append(info)
            new_on_page += 1

        except Exception as e:
            print(f" error: {e}")

    return new_on_page


# ---------------------------------------------------------------------------
# Main scraping function
# ---------------------------------------------------------------------------

def scrape_google_businesses(
    url: str | None = None,
    query: str = "restaurants",
    location: str | None = None,
    max_results: int = 50,
    headless: bool = True,
    slow_mo: int = 0,
    detail_scrape: bool = True,
) -> list[dict]:
    """
    Main scraping function.
    Provide either a full `url` or a `query` (+optional `location`).

    Two-phase approach:
      Phase 1 — scrape the initial udm=1 page
      Phase 2 — click "More places" → paginate through Local Finder pages
    """

    if not url:
        url = build_search_url(query, location)

    print(f"[*] Target URL: {url}")
    print(f"[*] Max results: {max_results}")

    results: list[dict] = []
    seen_cids: set[str] = set()

    with sync_playwright() as pw:
        launch_kwargs = dict(
            headless=headless,
            slow_mo=slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        # Auto-detect Chromium binary if Playwright can't find its own
        chrome_path = find_chromium_executable()
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
            print(f"[*] Using browser: {chrome_path}")

        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        page = context.new_page()
        if HAS_STEALTH:
            try:
                Stealth().apply_stealth_sync(page)
            except Exception:
                pass  # Stealth is optional; continue without it

        # Navigate to initial page
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        handle_consent(page)
        page.wait_for_timeout(random.randint(2000, 4000))

        # ---------------------------------------------------------------
        # Phase 1: Scrape the initial udm=1 page
        # ---------------------------------------------------------------
        print("[*] Phase 1: Scraping initial results page...")
        initial_count = scrape_page_of_results(
            page, seen_cids, results, max_results, detail_scrape,
        )
        print(f"[*] Phase 1 done: {initial_count} businesses from initial page")

        # ---------------------------------------------------------------
        # Phase 2: Click "More places" → paginate through Local Finder
        # ---------------------------------------------------------------
        if len(results) < max_results:
            # Try to enter the Local Finder by clicking "More places"
            more_btn = page.query_selector(
                "a:has-text('More places'), a:has-text('More businesses'), "
                "a[aria-label*='More places'], a[aria-label*='More businesses']"
            )
            if more_btn:
                print("[*] Phase 2: Clicking 'More places' to enter Local Finder...")
                more_btn.click()
                page.wait_for_timeout(random.randint(2500, 4000))
                handle_consent(page)  # May reappear after navigation
            else:
                print("[*] No 'More places' button found; trying pagination on current page...")

            # Paginate through Local Finder pages
            max_pages = (max_results // 10) + 5
            for page_num in range(max_pages):
                if len(results) >= max_results:
                    break

                print(f"[*] Page {page_num + 2}: scraping...")
                new_count = scrape_page_of_results(
                    page, seen_cids, results, max_results, detail_scrape,
                )

                if new_count == 0:
                    print("[*] No new results on this page, stopping pagination.")
                    break

                print(f"[*] Page {page_num + 2} done: {new_count} new businesses (total: {len(results)})")

                if len(results) >= max_results:
                    break

                # Navigate to next page
                next_btn = page.query_selector(
                    "a#pnnext, "
                    "a[aria-label='Next page'], "
                    "a[aria-label='Next'], "
                    "td.d6cvqb a[id='pnnext'], "
                    "a:has-text('Next')"
                )
                if not next_btn:
                    print("[*] No 'Next' button found, reached last page.")
                    break

                next_btn.click()
                page.wait_for_timeout(random.randint(2500, 4000))

        browser.close()

    print(f"\n[*] Successfully scraped {len(results)} businesses")
    return results


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "name", "category", "rating", "reviews_count", "phone",
    "address", "website", "socials", "hours", "price_range",
    "services", "description", "google_maps_url",
]


def save_csv(data: list[dict], filepath: str):
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)
    print(f"[*] Saved CSV: {filepath}")


def save_json(data: list[dict], filepath: str):
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[*] Saved JSON: {filepath}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape restaurant/business data from Google local results."
    )
    parser.add_argument("--query", "-q", default="restaurants",
                        help="Search query (e.g. 'restaurants', 'pizza places')")
    parser.add_argument("--location", "-l", default=None,
                        help="Location to search in (e.g. 'Swansea UK')")
    parser.add_argument("--url", "-u", default=None,
                        help="Full Google search URL (overrides --query/--location)")
    parser.add_argument("--max-results", "-m", type=int, default=50,
                        help="Maximum number of businesses to scrape (default 50)")
    parser.add_argument("--output", "-o", default="output/results",
                        help="Output file path without extension (default: output/results)")
    parser.add_argument("--format", "-f", choices=["csv", "json", "both"], default="both",
                        help="Output format (default: both)")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Run browser with visible window")
    parser.add_argument("--no-detail", action="store_true",
                        help="Skip opening individual business pages for extra data")

    args = parser.parse_args()

    headless = not args.no_headless

    data = scrape_google_businesses(
        url=args.url,
        query=args.query,
        location=args.location,
        max_results=args.max_results,
        headless=headless,
        detail_scrape=not args.no_detail,
    )

    if not data:
        print("[!] No results scraped. Google may have changed its page structure or blocked the request.")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_path = f"{args.output}_{timestamp}"

    if args.format in ("csv", "both"):
        save_csv(data, f"{base_path}.csv")
    if args.format in ("json", "both"):
        save_json(data, f"{base_path}.json")

    # Print summary table
    print(f"\n{'='*80}")
    print(f"{'Name':<30} {'Rating':>6} {'Phone':<16} {'Website'}")
    print(f"{'='*80}")
    for biz in data:
        name = (biz.get("name") or "")[:29]
        rating = biz.get("rating") or "-"
        phone = (biz.get("phone") or "-")[:15]
        website = (biz.get("website") or "-")[:40]
        print(f"{name:<30} {rating:>6} {phone:<16} {website}")


if __name__ == "__main__":
    main()
