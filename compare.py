"""
Compare Script — Runs both audit and enriched scrapers, then reports differences.

Usage:
    python compare.py --query "restaurants" --location "Swansea UK" --max-results 20
    python compare.py --url "https://www.google.com/search?q=restaurant&udm=1&..."
"""

import argparse
import json
import os
import sys
from datetime import datetime

from audit_scraper import audit_scrape
from scraper import scrape_google_businesses


def normalize_name(name: str) -> str:
    """Normalize a business name for fuzzy comparison."""
    return name.strip().lower().replace("'", "'").replace("\u2019", "'")


def compare(audit_results: list[dict], enriched_results: list[dict]):
    """Compare audit vs enriched results and print a report."""
    audit_names = [r["name"] for r in audit_results]
    enriched_names = [r.get("name") for r in enriched_results if r.get("name")]

    audit_normalized = {normalize_name(n): n for n in audit_names}
    enriched_normalized = {normalize_name(n): n for n in enriched_names}

    missing = []
    for norm, original in audit_normalized.items():
        if norm not in enriched_normalized:
            missing.append(original)

    extra = []
    for norm, original in enriched_normalized.items():
        if norm not in audit_normalized:
            extra.append(original)

    # Field completeness for enriched results
    fields = ["name", "address", "phone", "website", "rating", "reviews_count",
              "category", "hours", "google_maps_url", "socials"]
    field_counts = {f: 0 for f in fields}
    for r in enriched_results:
        for f in fields:
            val = r.get(f)
            if val and val != "" and val != []:
                field_counts[f] += 1

    total = len(enriched_results) or 1

    # Print report
    print("\n" + "=" * 70)
    print("COMPARISON REPORT")
    print("=" * 70)

    print(f"\nAudit scraper found:    {len(audit_results)} businesses")
    print(f"Enriched scraper found: {len(enriched_results)} businesses")

    if missing:
        print(f"\nMISSING from enriched scraper ({len(missing)}):")
        for i, name in enumerate(missing, 1):
            print(f"  {i}. {name}")
    else:
        print("\nNo missing businesses — enriched scraper captured everything!")

    if extra:
        print(f"\nEXTRA in enriched scraper ({len(extra)}):")
        for i, name in enumerate(extra, 1):
            print(f"  {i}. {name}")

    print(f"\nFIELD COMPLETENESS ({len(enriched_results)} businesses):")
    print(f"  {'Field':<18} {'Filled':>6} {'Empty':>6} {'Rate':>7}")
    print(f"  {'-'*40}")
    for f in fields:
        filled = field_counts[f]
        empty = len(enriched_results) - filled
        rate = (filled / total) * 100
        indicator = "OK" if rate > 80 else "LOW" if rate > 40 else "BAD"
        print(f"  {f:<18} {filled:>6} {empty:>6} {rate:>5.0f}%  {indicator}")

    match_rate = ((len(enriched_results) / max(len(audit_results), 1)) * 100)
    print(f"\nOVERALL CAPTURE RATE: {match_rate:.0f}% ({len(enriched_results)}/{len(audit_results)})")
    print("=" * 70)

    return missing


def main():
    parser = argparse.ArgumentParser(
        description="Run both scrapers and compare results."
    )
    parser.add_argument("--query", "-q", default="restaurants")
    parser.add_argument("--location", "-l", default=None)
    parser.add_argument("--url", "-u", default=None)
    parser.add_argument("--max-results", "-m", type=int, default=20)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--no-detail", action="store_true",
                        help="Skip detail panel scraping in enriched scraper")
    args = parser.parse_args()

    headless = not args.no_headless

    # Step 1: Run audit scraper (fast, no clicking)
    print("=" * 70)
    print("STEP 1: Running AUDIT scraper (list-only, no clicking)...")
    print("=" * 70)
    audit_results = audit_scrape(
        url=args.url,
        query=args.query,
        location=args.location,
        max_results=args.max_results,
        headless=headless,
    )

    # Step 2: Run enriched scraper
    print("\n" + "=" * 70)
    print("STEP 2: Running ENRICHED scraper (with detail panel clicks)...")
    print("=" * 70)
    enriched_results = scrape_google_businesses(
        url=args.url,
        query=args.query,
        location=args.location,
        max_results=args.max_results,
        headless=headless,
        detail_scrape=not args.no_detail,
    )

    # Step 3: Compare
    missing = compare(audit_results, enriched_results)

    # Save both results for manual inspection
    os.makedirs("output", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"output/compare_audit_{ts}.json", "w", encoding="utf-8") as f:
        json.dump(audit_results, f, indent=2, ensure_ascii=False)
    with open(f"output/compare_enriched_{ts}.json", "w", encoding="utf-8") as f:
        json.dump(enriched_results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to output/compare_audit_{ts}.json and output/compare_enriched_{ts}.json")

    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
