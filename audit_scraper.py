"""
Audit Scraper — Simple list-only scraper for comparison.

Does NOT click any cards or open detail panels. Just reads every visible
business name from the Google local results list, with pagination.
Use this to verify the enriched scraper isn't missing items.

Usage:
    python audit_scraper.py --query "restaurants" --location "Swansea UK" --max-results 50
    python audit_scraper.py --url "https://www.google.com/search?q=restaurant&udm=1&..."
"""

import argparse
import json
import os
import random
import re
import shutil
import sys
from datetime import datetime
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False


def find_chromium_executable() -> str | None:
    import pathlib
    pw_cache = pathlib.Path.home() / ".cache" / "ms-playwright"
    if pw_cache.exists():
        for d in sorted(pw_cache.iterdir(), reverse=True):
            if "chromium" in d.name and "headless_shell" not in d.name:
                candidate = d / "chrome-linux" / "chrome"
                if candidate.exists():
                    return str(candidate)
    for name in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
        path = shutil.which(name)
        if path:
            return path
    return None


def build_search_url(query: str, location: str | None = None) -> str:
    q = query
    if location:
        q = f"{query} in {location}"
    return f"https://www.google.com/search?{urlencode({'q': q, 'udm': '1'})}"


def handle_consent(page):
    for sel in [
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('Reject all')",
        "#L2AGLb",
        "button[aria-label*='Accept']",
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue


def extract_names_from_page(page) -> list[dict]:
    """Extract every business name + raw text from the current page using
    multiple strategies. No clicking, no DOM mutation — read-only."""
    results = []

    # Strategy 1: Use JS to find all heading elements inside the results area
    # and grab each card's full text
    items = page.evaluate("""() => {
        const cards = [];
        // Try multiple approaches to find card containers

        // Approach A: data-cid divs
        let containers = document.querySelectorAll('div[data-cid]');

        // Approach B: rllt__link divs
        if (containers.length === 0)
            containers = document.querySelectorAll('div.rllt__link');

        // Approach C: vwVdIc links
        if (containers.length === 0)
            containers = document.querySelectorAll('a.vwVdIc');

        // Approach D: data-ludocid
        if (containers.length === 0)
            containers = document.querySelectorAll('div[data-ludocid]');

        // Approach E: find all role=heading, walk up to container
        if (containers.length === 0) {
            const headings = document.querySelectorAll('[role="heading"]');
            const parents = [];
            for (const h of headings) {
                let p = h.parentElement;
                for (let i = 0; i < 5 && p; i++) {
                    const t = p.innerText || '';
                    if (t.length > 20 && t.length < 2000 && t.includes('\\n')) {
                        parents.push(p);
                        break;
                    }
                    p = p.parentElement;
                }
            }
            // Deduplicate
            containers = parents.filter(c =>
                !parents.some(other => other !== c && other.contains(c))
            );
        }

        for (const el of containers) {
            const heading = el.querySelector('[role="heading"], h3');
            const name = heading ? heading.innerText.trim() : null;
            const fullText = el.innerText.trim();
            const firstLine = fullText.split('\\n')[0].trim();
            cards.push({
                name: name || firstLine || null,
                raw_text: fullText.substring(0, 500)
            });
        }
        return cards;
    }""")

    if items:
        for item in items:
            if item.get("name"):
                results.append(item)

    return results


def audit_scrape(
    url: str | None = None,
    query: str = "restaurants",
    location: str | None = None,
    max_results: int = 200,
    headless: bool = True,
) -> list[dict]:
    if not url:
        url = build_search_url(query, location)

    print(f"[AUDIT] URL: {url}")
    print(f"[AUDIT] Max results: {max_results}")

    all_results = []
    seen_names: set[str] = set()

    with sync_playwright() as pw:
        launch_kwargs = dict(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        chrome_path = find_chromium_executable()
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path

        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        page = context.new_page()
        if HAS_STEALTH:
            try:
                Stealth().apply_stealth_sync(page)
            except Exception:
                pass

        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        handle_consent(page)
        page.wait_for_timeout(random.randint(2000, 3000))

        # Save debug snapshot of initial page
        os.makedirs("debug", exist_ok=True)
        page.screenshot(path="debug/audit_initial.png", full_page=True)

        # Phase 1: Initial page
        print("[AUDIT] Phase 1: Reading initial page...")
        items = extract_names_from_page(page)
        for item in items:
            name = item["name"]
            if name not in seen_names:
                seen_names.add(name)
                all_results.append(item)
        print(f"[AUDIT] Phase 1: {len(all_results)} businesses found")

        # Phase 2: More places / pagination
        if len(all_results) < max_results:
            more_btn = None
            for sel in [
                "a:has-text('More places')",
                "a:has-text('More businesses')",
                "a[aria-label*='More places']",
                "a[aria-label*='More businesses']",
            ]:
                try:
                    more_btn = page.query_selector(sel)
                    if more_btn and more_btn.is_visible():
                        break
                    more_btn = None
                except Exception:
                    continue

            if more_btn:
                print("[AUDIT] Clicking 'More places'...")
                more_btn.click()
                page.wait_for_timeout(random.randint(2500, 4000))
                handle_consent(page)

            max_pages = (max_results // 10) + 5
            for page_num in range(max_pages):
                if len(all_results) >= max_results:
                    break

                items = extract_names_from_page(page)
                new_count = 0
                for item in items:
                    name = item["name"]
                    if name not in seen_names:
                        seen_names.add(name)
                        all_results.append(item)
                        new_count += 1

                if new_count == 0:
                    # Try scroll
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)
                    items = extract_names_from_page(page)
                    for item in items:
                        name = item["name"]
                        if name not in seen_names:
                            seen_names.add(name)
                            all_results.append(item)
                            new_count += 1
                    if new_count == 0:
                        print(f"[AUDIT] Page {page_num + 2}: no new results, stopping.")
                        break

                print(f"[AUDIT] Page {page_num + 2}: {new_count} new (total: {len(all_results)})")

                if len(all_results) >= max_results:
                    break

                # Next page
                next_btn = None
                for sel in [
                    "a#pnnext",
                    "a[aria-label='Next page']",
                    "a[aria-label='Next']",
                    "td.d6cvqb a[id='pnnext']",
                    "a:has-text('Next')",
                ]:
                    try:
                        next_btn = page.query_selector(sel)
                        if next_btn and next_btn.is_visible():
                            break
                        next_btn = None
                    except Exception:
                        continue

                if not next_btn:
                    print("[AUDIT] No 'Next' button, reached last page.")
                    break

                next_btn.click()
                page.wait_for_timeout(random.randint(2500, 4000))

        browser.close()

    print(f"\n[AUDIT] Total: {len(all_results)} businesses found")
    return all_results


def main():
    parser = argparse.ArgumentParser(description="Audit scraper — list-only, no clicking.")
    parser.add_argument("--query", "-q", default="restaurants")
    parser.add_argument("--location", "-l", default=None)
    parser.add_argument("--url", "-u", default=None)
    parser.add_argument("--max-results", "-m", type=int, default=200)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    data = audit_scrape(
        url=args.url,
        query=args.query,
        location=args.location,
        max_results=args.max_results,
        headless=not args.no_headless,
    )

    if not data:
        print("[AUDIT] No results found!")
        sys.exit(1)

    # Save results
    os.makedirs("output", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = f"output/audit_{ts}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[AUDIT] Saved: {filepath}")

    # Print numbered list
    print(f"\n{'='*60}")
    print(f"{'#':>4}  {'Name'}")
    print(f"{'='*60}")
    for i, item in enumerate(data, 1):
        print(f"{i:4d}  {item['name']}")


if __name__ == "__main__":
    main()
