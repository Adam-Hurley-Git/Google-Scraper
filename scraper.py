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
# Core scraper
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

    # --- Rating ---
    for sel in ["span.yi40Hd", "span[aria-label*='star']", "span.Fam1ne"]:
        el = element.query_selector(sel)
        if el:
            label = el.get_attribute("aria-label") or el.inner_text()
            m = re.search(r'([\d.]+)', label)
            if m:
                info["rating"] = m.group(1)
                break

    # --- Reviews count ---
    for sel in ["span.RDApEe", "span.hqzQac"]:
        el = element.query_selector(sel)
        if el:
            txt = el.inner_text()
            m = re.search(r'([\d,]+)', txt)
            if m:
                info["reviews_count"] = m.group(1).replace(",", "")
                break
    # Also try aria-label on rating elements
    if not info["reviews_count"]:
        rating_el = element.query_selector("span[aria-label*='review']")
        if rating_el:
            label = rating_el.get_attribute("aria-label") or ""
            m = re.search(r'([\d,]+)\s*review', label)
            if m:
                info["reviews_count"] = m.group(1).replace(",", "")

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
    for sel in [".rllt__details div:has-text('Open')", "span:has-text('Open')", "span:has-text('Closed')"] :
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


def scrape_detail_page(page, url: str, timeout: int = 8000) -> dict:
    """Open a business detail panel and extract extra data (website, socials, phone, address)."""
    extra = {
        "website": None,
        "phone": None,
        "address": None,
        "socials": [],
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_timeout(random.randint(1500, 3000))

        # Website
        website_el = page.query_selector("a[data-attrid='visit_website'], a[href]:has-text('Website')")
        if website_el:
            href = website_el.get_attribute("href") or ""
            if href and "google" not in href:
                extra["website"] = href

        # Phone
        phone_el = page.query_selector("[data-attrid*='phone'] span, a[href^='tel:']")
        if phone_el:
            txt = phone_el.inner_text().strip()
            if txt:
                extra["phone"] = txt

        # Address
        addr_el = page.query_selector("[data-attrid*='address'] span")
        if addr_el:
            extra["address"] = addr_el.inner_text().strip()

        # Social links
        for link in page.query_selector_all("a[href]"):
            href = link.get_attribute("href") or ""
            for domain in ["facebook.com", "instagram.com", "twitter.com", "x.com",
                           "tiktok.com", "linkedin.com", "youtube.com", "yelp.com"]:
                if domain in href:
                    extra["socials"].append(href)
                    break

    except Exception as e:
        print(f"  [!] Could not load detail page: {e}")

    return extra


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
    """

    if not url:
        url = build_search_url(query, location)

    print(f"[*] Target URL: {url}")
    print(f"[*] Max results: {max_results}")

    results = []

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

        # Navigate
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Handle consent / cookie banner
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

        page.wait_for_timeout(random.randint(2000, 4000))

        # Scroll to load more results
        prev_count = 0
        for scroll_round in range(20):
            # Identify business listing containers
            cards = page.query_selector_all(
                "div.rllt__link, div[jscontroller] div[data-cid], "
                "div[class*='VkpGBb'], div.uMdZh, div[data-hveid] div[data-ved]"
            )
            if len(cards) >= max_results:
                break
            if len(cards) == prev_count and scroll_round > 2:
                # Try clicking "More places" / "Next" button
                more_btn = page.query_selector(
                    "a:has-text('More places'), a:has-text('Next'), "
                    "a[aria-label='Next'], span:has-text('More results')"
                )
                if more_btn:
                    more_btn.click()
                    page.wait_for_timeout(random.randint(2000, 3500))
                else:
                    break
            prev_count = len(cards)
            page.evaluate("window.scrollBy(0, 800)")
            page.wait_for_timeout(random.randint(800, 1500))

        # Re-query all listing cards after scrolling
        cards = page.query_selector_all(
            "div.rllt__link, div[jscontroller] div[data-cid], "
            "div[class*='VkpGBb'], div.uMdZh"
        )

        # If that didn't find results, try broader selectors
        if not cards:
            cards = page.query_selector_all("div[data-hveid]")
        if not cards:
            # Last resort: grab the whole local results container
            local_container = page.query_selector("#local-search-content, #rso")
            if local_container:
                cards = local_container.query_selector_all(":scope > div")

        print(f"[*] Found {len(cards)} listing elements on page")

        for i, card in enumerate(cards[:max_results]):
            print(f"  [{i+1}/{min(len(cards), max_results)}] Scraping...", end="")
            try:
                info = scrape_business_panel(page, card)
                if not info["name"]:
                    print(" skipped (no name)")
                    continue

                # Try to get a detail link
                detail_link = card.query_selector("a[href*='/search?']")
                if not detail_link:
                    detail_link = card.query_selector("a[href]")

                if detail_scrape and detail_link:
                    href = detail_link.get_attribute("href") or ""
                    if href and not href.startswith("javascript"):
                        if not href.startswith("http"):
                            href = f"https://www.google.com{href}"
                        extra = scrape_detail_page(page, href)
                        # Merge — prefer detail-page data when available
                        if extra["website"]:
                            info["website"] = extra["website"]
                        if extra["phone"]:
                            info["phone"] = extra["phone"]
                        if extra["address"]:
                            info["address"] = extra["address"]
                        if extra["socials"]:
                            info["socials"] = extra["socials"]
                        # Navigate back to results list
                        page.go_back(wait_until="domcontentloaded", timeout=10_000)
                        page.wait_for_timeout(random.randint(1000, 2000))

                # Ensure socials is a semicolon-joined string for CSV
                if isinstance(info.get("socials"), list):
                    info["socials"] = "; ".join(info["socials"]) if info["socials"] else ""
                if isinstance(info.get("services"), list):
                    info["services"] = "; ".join(info["services"]) if info["services"] else ""

                print(f" {info['name']}")
                results.append(info)

            except Exception as e:
                print(f" error: {e}")

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
