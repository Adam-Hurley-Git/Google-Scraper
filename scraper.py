"""
Google Local Business Scraper

Scrapes business information from Google Search local/business results using
Playwright with stealth mode. Extracts: name, address, phone, website, rating,
reviews count, hours, category, and more.

Usage:
    python scraper.py --query "restaurants" --location "Swansea UK" --max-results 50
    python scraper.py --url "https://www.google.com/search?q=restaurant&udm=1&..."
    python scraper.py --query "restaurants" --location "London" --debug --no-headless
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
from urllib.parse import urlencode

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
# Debug infrastructure
# ---------------------------------------------------------------------------

_debug_counter = 0


def save_debug_snapshot(page, label: str = "snapshot"):
    """Save a screenshot and HTML dump for debugging."""
    global _debug_counter
    _debug_counter += 1
    os.makedirs("debug", exist_ok=True)
    prefix = f"debug/{_debug_counter:03d}_{label}"
    try:
        page.screenshot(path=f"{prefix}.png", full_page=True)
        print(f"  [debug] Screenshot saved: {prefix}.png")
    except Exception as e:
        print(f"  [debug] Screenshot failed: {e}")
    try:
        html = page.content()
        with open(f"{prefix}.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  [debug] HTML saved: {prefix}.html")
    except Exception as e:
        print(f"  [debug] HTML save failed: {e}")


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
# Social domains for link detection
# ---------------------------------------------------------------------------

SOCIAL_DOMAINS = [
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "linkedin.com", "youtube.com", "yelp.com",
]


# ---------------------------------------------------------------------------
# Multi-strategy card detection
# ---------------------------------------------------------------------------

CARD_STRATEGIES = [
    # (label, selector)
    ("data-cid", "div[data-cid]"),
    ("rllt__link", "div.rllt__link"),
    ("vwVdIc", "a.vwVdIc"),
    ("data-ludocid", "div[data-ludocid]"),
    ("data-ved-jscontroller", "div[jscontroller][data-ved]"),
]


def find_business_cards(page, debug: bool = False) -> tuple[str, list]:
    """Try multiple selector strategies to find business listing cards.

    Returns (strategy_label, list_of_element_handles).
    """
    for label, selector in CARD_STRATEGIES:
        try:
            cards = page.query_selector_all(selector)
            if cards and len(cards) > 0:
                print(f"  [+] Card strategy '{label}' matched {len(cards)} elements")
                return label, cards
        except Exception:
            continue

    # JS-based fallback: find elements that contain a heading (business name)
    try:
        cards = page.evaluate("""() => {
            // Look for elements that have a role=heading descendant
            const candidates = [];
            const headings = document.querySelectorAll('[role="heading"]');
            for (const h of headings) {
                let parent = h.parentElement;
                // Walk up to find a reasonable container (3-5 levels)
                for (let i = 0; i < 5 && parent; i++) {
                    const text = parent.innerText || '';
                    // A business card has a name + some info (multiple lines)
                    if (text.length > 20 && text.length < 2000 && text.includes('\\n')) {
                        candidates.push(parent);
                        break;
                    }
                    parent = parent.parentElement;
                }
            }
            // Deduplicate by removing children of other candidates
            const unique = candidates.filter(c =>
                !candidates.some(other => other !== c && other.contains(c))
            );
            // Tag them with a custom attribute so we can query them
            unique.forEach((el, i) => el.setAttribute('data-scraper-card', i.toString()));
            return unique.length;
        }""")
        if cards and cards > 0:
            card_elements = page.query_selector_all("[data-scraper-card]")
            if card_elements:
                print(f"  [+] JS fallback strategy matched {len(card_elements)} elements")
                return "js-fallback", card_elements
    except Exception as e:
        if debug:
            print(f"  [debug] JS fallback error: {e}")

    print("  [-] No card detection strategy found any results")
    return "none", []


def get_card_id(element) -> str:
    """Generate a unique identifier for a card to prevent duplicates."""
    # Only use truly stable unique IDs (data-cid, data-ludocid)
    # Do NOT use data-ved — it's a tracking token, not a business ID
    for attr in ["data-cid", "data-ludocid"]:
        try:
            val = element.get_attribute(attr)
            if val:
                return f"{attr}:{val}"
        except Exception:
            continue
    # Fallback: use the business name (most reliable dedup key)
    try:
        # Try heading element first for clean name
        heading = element.query_selector("[role='heading'], h3")
        if heading:
            name = heading.inner_text().strip()
            if name:
                return f"name:{name}"
    except Exception:
        pass
    # Last resort: first line of text
    try:
        text = element.inner_text().split("\n")[0].strip()
        if text:
            return f"text:{text}"
    except Exception:
        pass
    return f"idx:{id(element)}"


# ---------------------------------------------------------------------------
# Listing card extraction
# ---------------------------------------------------------------------------

def scrape_business_card(page, element) -> dict:
    """Extract all available data from a single business listing card element.

    Uses two layers: stable CSS selectors first, then text parsing fallback.
    """
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

    # --- Layer 1: CSS selectors ---

    # Name
    for sel in ["[role='heading']", "h3", "[aria-level]", "div[role='heading']"]:
        try:
            el = element.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if txt and len(txt) < 100:
                    info["name"] = txt
                    break
        except Exception:
            continue

    # Rating (aria-label based)
    for sel in [
        "span[aria-label*='star']",
        "span[aria-label*='Rated']",
        "span[aria-label*='rating']",
    ]:
        try:
            el = element.query_selector(sel)
            if el:
                label = el.get_attribute("aria-label") or el.inner_text()
                m = re.search(r'([\d.]+)', label)
                if m:
                    info["rating"] = m.group(1)
                    break
        except Exception:
            continue

    # Reviews count (aria-label based)
    try:
        rating_el = element.query_selector("span[aria-label*='review']")
        if rating_el:
            label = rating_el.get_attribute("aria-label") or ""
            m = re.search(r'([\d,]+)\s*review', label)
            if m:
                info["reviews_count"] = m.group(1).replace(",", "")
    except Exception:
        pass

    # Website link (non-Google external link)
    try:
        links = element.query_selector_all("a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            if href.startswith("http") and "google" not in href and "gstatic" not in href:
                info["website"] = href
                break
    except Exception:
        pass

    # Google Maps link
    try:
        maps_link = element.query_selector("a[href*='maps.google'], a[href*='google.com/maps']")
        if maps_link:
            info["google_maps_url"] = maps_link.get_attribute("href")
    except Exception:
        pass

    # --- Layer 2: Text parsing fallback ---
    try:
        full_text = element.inner_text()
    except Exception:
        full_text = ""

    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    # Name fallback
    if not info["name"] and lines:
        # First substantial line is usually the name
        for line in lines:
            if len(line) > 1 and len(line) < 80 and not re.match(r'^[\d.]+$', line):
                info["name"] = line
                break

    # Rating fallback
    if not info["rating"]:
        m = re.search(r'(\d\.\d)\s*(?:\(|star|★)', full_text)
        if m:
            info["rating"] = m.group(1)

    # Reviews count fallback
    if not info["reviews_count"]:
        # Try formats: (1,300), (1.3k), (1.3K), (523)
        m = re.search(r'\(([\d,.]+[kKmM]?)\)', full_text)
        if m:
            raw = m.group(1).replace(",", "")
            # Convert shorthand like 1.3k -> 1300
            km = re.match(r'^([\d.]+)([kKmM])$', raw)
            if km:
                num = float(km.group(1))
                suffix = km.group(2).lower()
                if suffix == 'k':
                    info["reviews_count"] = str(int(num * 1000))
                elif suffix == 'm':
                    info["reviews_count"] = str(int(num * 1000000))
            else:
                info["reviews_count"] = raw

    # Category: short line after name that isn't address/phone/rating
    if not info["category"] and info["name"] and len(lines) > 1:
        name_idx = None
        for i, line in enumerate(lines):
            if line == info["name"]:
                name_idx = i
                break
        if name_idx is not None:
            # Check the next few lines after the name for a category
            for offset in range(1, min(4, len(lines) - name_idx)):
                candidate = lines[name_idx + offset]
                # Skip if it looks like a rating (e.g. "4.6", "4.6(1.3k)")
                if re.match(r'^\d\.\d', candidate):
                    continue
                # Skip if it looks like an address
                if re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Blvd|Street|Road|Neath|High)', candidate, re.I):
                    continue
                # Skip phone numbers
                if extract_phone(candidate):
                    continue
                # Skip if it contains pricing symbols only
                if re.match(r'^[$£€·\s]+$', candidate):
                    continue
                # Good category candidate: short, text-like
                if len(candidate) < 40 and re.search(r'[a-zA-Z]', candidate):
                    info["category"] = candidate
                    break

    # Phone
    if not info["phone"]:
        info["phone"] = extract_phone(full_text)

    # Address (heuristic: line with street-like pattern)
    for line in lines:
        if re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Blvd|Dr|Ln|Way|Street|Road|Avenue|Place|Pl|Ct|Sq|High|Terr)', line, re.I):
            info["address"] = line
            break
    if not info["address"]:
        for line in lines:
            if re.search(r'\b[A-Z]{1,2}\d[\dA-Z]?\s?\d[A-Z]{2}\b', line):  # UK postcode
                info["address"] = line
                break
            if re.search(r'\b\d{5}(-\d{4})?\b', line):  # US zip
                info["address"] = line
                break

    # Hours (match "Open", "Opens", "Closed", "Closes", time patterns)
    for line in lines:
        if re.search(r'(Opens?\b|Closes?\b|Hours|Hrs|\d{1,2}:\d{2}\s*(am|pm|AM|PM)|\d{1,2}\s*(am|pm|AM|PM))', line):
            # Skip lines that are clearly not hours (e.g. addresses, names)
            if not re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Street|Road)', line, re.I):
                info["hours"] = line
                break

    # Price range
    m = re.search(r'([$£€]{1,4})\s*[-–]\s*([$£€]{1,4})', full_text)
    if m:
        info["price_range"] = f"{m.group(1)}-{m.group(2)}"
    else:
        m = re.search(r'([$£€]{1,4})(?:\s|$)', full_text)
        if m and len(m.group(1)) <= 4:
            info["price_range"] = m.group(1)

    # Services
    for kw in ["Dine-in", "Takeaway", "Takeout", "Delivery", "Kerbside pickup", "Curbside pickup", "In-store pickup"]:
        if kw.lower() in full_text.lower():
            info["services"].append(kw)

    return info


# ---------------------------------------------------------------------------
# Detail panel extraction (click card → extract → close)
# ---------------------------------------------------------------------------

def scrape_detail_panel(page, card, debug: bool = False, timeout: int = 8000) -> dict:
    """Click a business card to open its detail panel, extract enriched data, then close it."""
    extra = {
        "website": None,
        "phone": None,
        "address": None,
        "hours": None,
        "reviews_count": None,
        "google_maps_url": None,
        "socials": [],
    }

    url_before = page.url

    try:
        # 1. Click the card
        card.click()
        page.wait_for_timeout(random.randint(1500, 2500))

        navigated = page.url != url_before

        # 2. Wait for detail content to appear
        try:
            page.wait_for_selector(
                "a[href^='tel:'], [data-attrid*='phone'], [data-attrid*='address'], "
                "a[data-attrid='visit_website'], a:has-text('Website')",
                timeout=timeout,
            )
        except Exception:
            pass  # Panel may not have all fields

        page.wait_for_timeout(random.randint(500, 1000))

        if debug:
            save_debug_snapshot(page, "detail_panel")

        # 3. Extract website
        for sel in [
            "a:has-text('Website')",
            "a[data-attrid='visit_website']",
            "[data-attrid='visit_website'] a[href]",
            "a.n1obkb[href]",
        ]:
            try:
                website_el = page.query_selector(sel)
                if website_el:
                    href = website_el.get_attribute("href") or ""
                    if href and "google" not in href:
                        extra["website"] = href
                        break
            except Exception:
                continue

        # Fallback: first non-Google link in the detail area
        if not extra["website"]:
            try:
                all_links = page.query_selector_all("a[href^='http']")
                for link in all_links:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip().lower()
                    if href and "google" not in href and "gstatic" not in href:
                        # Prefer links that say "website" or are prominent
                        if "website" in text or "site" in text:
                            extra["website"] = href
                            break
            except Exception:
                pass

        # 4. Extract phone
        for sel in [
            "a[href^='tel:']",
            "[data-attrid*='phone'] span.LrzXr",
            "[data-attrid*='phone'] span",
        ]:
            try:
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
            except Exception:
                continue

        # 5. Extract address
        for sel in [
            "[data-attrid*='address'] span.LrzXr",
            "[data-attrid*='address'] span",
        ]:
            try:
                addr_el = page.query_selector(sel)
                if addr_el:
                    txt = addr_el.inner_text().strip()
                    if txt:
                        extra["address"] = txt
                        break
            except Exception:
                continue

        # Address fallback: text parsing from panel
        if not extra["address"]:
            try:
                panel_text = page.inner_text("body")
                for line in panel_text.split("\n"):
                    line = line.strip()
                    if re.search(r'\d+\s+\w+\s+(St|Ave|Rd|Blvd|Dr|Ln|Way|Street|Road|Avenue|Place|Pl|Ct|Sq|High|Terr)', line, re.I):
                        extra["address"] = line
                        break
            except Exception:
                pass

        # 6. Extract hours from detail panel
        for sel in [
            "[data-attrid*='hours'] span",
            "[data-attrid*='hours']",
            "span:has-text('Opens')",
            "span:has-text('Closes')",
            "span:has-text('Open')",
            "span:has-text('Closed')",
        ]:
            try:
                hours_el = page.query_selector(sel)
                if hours_el:
                    txt = hours_el.inner_text().strip()
                    if txt and re.search(r'(Opens?\b|Closes?\b|hours|:\d{2}|am|pm|AM|PM)', txt, re.I):
                        extra["hours"] = txt.split("\n")[0].strip()
                        break
            except Exception:
                continue

        # 7. Extract reviews count from detail panel
        for sel in [
            "span[aria-label*='review']",
            "a:has-text('reviews')",
            "a:has-text('review')",
        ]:
            try:
                rev_el = page.query_selector(sel)
                if rev_el:
                    txt = (rev_el.get_attribute("aria-label") or rev_el.inner_text()).strip()
                    m = re.search(r'([\d,.]+[kKmM]?)\s*review', txt)
                    if m:
                        raw = m.group(1).replace(",", "")
                        km = re.match(r'^([\d.]+)([kKmM])$', raw)
                        if km:
                            num = float(km.group(1))
                            suffix = km.group(2).lower()
                            if suffix == 'k':
                                extra["reviews_count"] = str(int(num * 1000))
                            elif suffix == 'm':
                                extra["reviews_count"] = str(int(num * 1000000))
                        else:
                            extra["reviews_count"] = raw
                        break
            except Exception:
                continue

        # 8. Extract Google Maps URL from detail panel
        try:
            maps_link = page.query_selector(
                "a[href*='maps.google'], a[href*='google.com/maps'], "
                "a[data-url*='maps.google'], a[data-url*='google.com/maps']"
            )
            if maps_link:
                extra["google_maps_url"] = maps_link.get_attribute("href") or maps_link.get_attribute("data-url")
        except Exception:
            pass

        # 10. Extract social links
        seen_socials: set[str] = set()
        try:
            for link in page.query_selector_all(
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
        except Exception:
            pass

        # 11. Close the panel / go back
        if navigated:
            page.go_back(wait_until="domcontentloaded", timeout=10_000)
            page.wait_for_timeout(random.randint(1000, 2000))
        else:
            page.keyboard.press("Escape")
            page.wait_for_timeout(random.randint(800, 1500))
            # Check if panel closed by looking for close button
            try:
                close_btn = page.query_selector("button[aria-label='Close'], g-back-button")
                if close_btn:
                    close_btn.click()
                    page.wait_for_timeout(random.randint(500, 1000))
            except Exception:
                pass

    except Exception as e:
        print(f"  [!] Detail panel error: {e}")
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
    consent_selectors = [
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('Reject all')",
        "#L2AGLb",
        "button[aria-label*='Accept']",
        "form[action*='consent'] button",
    ]
    for sel in consent_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue


def merge_detail(info: dict, extra: dict):
    """Merge detail panel data into listing info, preferring detail data."""
    if extra.get("website"):
        info["website"] = extra["website"]
    if extra.get("phone"):
        info["phone"] = extra["phone"]
    if extra.get("address"):
        info["address"] = extra["address"]
    if extra.get("hours"):
        info["hours"] = extra["hours"]
    if extra.get("reviews_count"):
        info["reviews_count"] = extra["reviews_count"]
    if extra.get("google_maps_url"):
        info["google_maps_url"] = extra["google_maps_url"]
    if extra.get("socials"):
        info["socials"] = extra["socials"]


def relocate_card(page, strategy_label: str, card_id: str, cards_selector_used: str, index: int = -1):
    """Re-find a card element after DOM mutations (e.g. after detail panel close).

    Tries: data attribute query, then ID matching across all cards, then index fallback.
    """
    # If we have a data attribute ID, re-query directly
    if ":" in card_id:
        attr_name, attr_val = card_id.split(":", 1)
        if attr_name in ("data-cid", "data-ludocid", "data-scraper-card"):
            try:
                el = page.query_selector(f'[{attr_name}="{attr_val}"]')
                if el:
                    return el
            except Exception:
                pass

    # Re-query all cards and match by ID
    try:
        all_cards = page.query_selector_all(cards_selector_used)
        for card in all_cards:
            if get_card_id(card) == card_id:
                return card
        # Index-based fallback: if the ID changed but card is still at same position
        if 0 <= index < len(all_cards):
            return all_cards[index]
    except Exception:
        pass

    return None


def scrape_page_of_results(
    page, seen_ids: set, results: list, max_results: int,
    detail_scrape: bool, debug: bool,
):
    """Scrape all business cards on the current page, appending to results."""
    strategy_label, cards = find_business_cards(page, debug=debug)

    if not cards:
        if debug:
            save_debug_snapshot(page, "no_cards_found")
        else:
            # Always save debug snapshot when zero cards found
            save_debug_snapshot(page, "no_cards_auto")
        return 0

    # Determine the selector used for this strategy (for relocating cards later)
    selector_map = {label: sel for label, sel in CARD_STRATEGIES}
    cards_selector = selector_map.get(strategy_label, "[data-scraper-card]")

    # Collect card identifiers upfront (with index for fallback relocation)
    card_refs = []
    for idx, card in enumerate(cards):
        card_id = get_card_id(card)
        if card_id not in seen_ids:
            card_refs.append((card_id, idx))
            seen_ids.add(card_id)

    print(f"  [*] {len(card_refs)} new cards to scrape (of {len(cards)} found)")
    new_on_page = 0

    for card_id, card_idx in card_refs:
        if len(results) >= max_results:
            break

        # Re-find the card (safe against stale references)
        card = relocate_card(page, strategy_label, card_id, cards_selector, index=card_idx)
        if not card:
            print(f"  [!] Could not relocate card {card_id}, retrying...")
            # One more attempt: re-find all cards and use index
            try:
                all_cards = page.query_selector_all(cards_selector)
                if card_idx < len(all_cards):
                    card = all_cards[card_idx]
            except Exception:
                pass
            if not card:
                print(f"  [!] Skipping card (could not relocate after retry)")
                continue

        count = len(results) + 1
        print(f"  [{count}] Scraping...", end="")

        try:
            info = scrape_business_card(page, card)
            if not info["name"]:
                # Try harder: get first line of text as name
                try:
                    text = card.inner_text().strip()
                    first_line = text.split("\n")[0].strip()
                    if first_line and len(first_line) < 80:
                        info["name"] = first_line
                except Exception:
                    pass
            if not info["name"]:
                print(" skipped (no name could be extracted)")
                continue

            # Detail panel scraping
            if detail_scrape:
                card = relocate_card(page, strategy_label, card_id, cards_selector, index=card_idx)
                if card:
                    extra = scrape_detail_panel(page, card, debug=debug)
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
    debug: bool = False,
) -> list[dict]:
    """
    Main scraping function.
    Provide either a full `url` or a `query` (+optional `location`).

    Two-phase approach:
      Phase 1 -- scrape the initial udm=1 page
      Phase 2 -- click "More places" or paginate through results
    """

    if not url:
        url = build_search_url(query, location)

    print(f"[*] Target URL: {url}")
    print(f"[*] Max results: {max_results}")

    results: list[dict] = []
    seen_ids: set[str] = set()

    with sync_playwright() as pw:
        launch_kwargs = dict(
            headless=headless,
            slow_mo=slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
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

        # Navigate to initial page
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        handle_consent(page)
        page.wait_for_timeout(random.randint(2000, 4000))

        if debug:
            save_debug_snapshot(page, "initial_page")

        # ---------------------------------------------------------------
        # Phase 1: Scrape the initial udm=1 page
        # ---------------------------------------------------------------
        print("[*] Phase 1: Scraping initial results page...")
        initial_count = scrape_page_of_results(
            page, seen_ids, results, max_results, detail_scrape, debug,
        )
        print(f"[*] Phase 1 done: {initial_count} businesses from initial page")

        # ---------------------------------------------------------------
        # Phase 2: Paginate through more results
        # ---------------------------------------------------------------
        if len(results) < max_results:
            # Try to enter the Local Finder by clicking "More places"
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
                print("[*] Phase 2: Clicking 'More places' to enter Local Finder...")
                more_btn.click()
                page.wait_for_timeout(random.randint(2500, 4000))
                handle_consent(page)
                if debug:
                    save_debug_snapshot(page, "more_places")
            else:
                print("[*] No 'More places' button found; trying pagination on current page...")

            # Paginate through pages
            max_pages = (max_results // 10) + 5
            for page_num in range(max_pages):
                if len(results) >= max_results:
                    break

                print(f"[*] Page {page_num + 2}: scraping...")
                new_count = scrape_page_of_results(
                    page, seen_ids, results, max_results, detail_scrape, debug,
                )

                if new_count == 0:
                    # Try scrolling to load more
                    prev_count = len(results)
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(2000)
                        scroll_count = scrape_page_of_results(
                            page, seen_ids, results, max_results, detail_scrape, debug,
                        )
                        if scroll_count > 0:
                            print(f"[*] Scroll loaded {scroll_count} more results")
                            continue
                    except Exception:
                        pass
                    print("[*] No new results on this page, stopping pagination.")
                    break

                print(f"[*] Page {page_num + 2} done: {new_count} new businesses (total: {len(results)})")

                if len(results) >= max_results:
                    break

                # Navigate to next page
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
                    print("[*] No 'Next' button found, reached last page.")
                    break

                next_btn.click()
                page.wait_for_timeout(random.randint(2500, 4000))

                if debug:
                    save_debug_snapshot(page, f"page_{page_num + 2}")

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
    parser.add_argument("--debug", action="store_true",
                        help="Save screenshots and HTML dumps to debug/ directory")

    args = parser.parse_args()

    headless = not args.no_headless

    data = scrape_google_businesses(
        url=args.url,
        query=args.query,
        location=args.location,
        max_results=args.max_results,
        headless=headless,
        detail_scrape=not args.no_detail,
        debug=args.debug,
    )

    if not data:
        print("[!] No results scraped. Google may have changed its page structure or blocked the request.")
        print("[!] Try running with --debug --no-headless to inspect what Google is returning.")
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
