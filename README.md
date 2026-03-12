# Google Business Scraper

A simple Python scraper that extracts business information from Google Search local results using Playwright with stealth mode.

## Data Extracted

| Field | Description |
|-------|-------------|
| Name | Business name |
| Category | Business type (e.g., Restaurant, Cafe) |
| Rating | Star rating (e.g., 4.5) |
| Reviews Count | Number of Google reviews |
| Phone | Phone number |
| Address | Street address |
| Website | Business website URL |
| Socials | Social media links (Facebook, Instagram, etc.) |
| Hours | Operating hours / open status |
| Price Range | Price level (e.g., ££) |
| Services | Dine-in, Takeaway, Delivery, etc. |
| Google Maps URL | Direct link to Google Maps listing |

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browsers (first time only)
playwright install chromium
```

## Usage

### Search by query + location
```bash
python scraper.py --query "restaurants" --location "Swansea UK" --max-results 30
```

### Use a specific Google search URL
```bash
python scraper.py --url "https://www.google.com/search?q=restaurant&udm=1&..." --max-results 50
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--query, -q` | Search query | `restaurants` |
| `--location, -l` | Location to search in | None |
| `--url, -u` | Full Google search URL (overrides query/location) | None |
| `--max-results, -m` | Max businesses to scrape | 50 |
| `--output, -o` | Output file path (no extension) | `output/results` |
| `--format, -f` | Output format: csv, json, or both | `both` |
| `--no-headless` | Show browser window | False |
| `--no-detail` | Skip detail page scraping (faster but less data) | False |

### Examples

```bash
# Quick scrape, list view only (no detail pages), CSV output
python scraper.py -q "pizza" -l "London" -m 20 --no-detail -f csv

# Full scrape with visible browser for debugging
python scraper.py -q "restaurants" -l "Cardiff UK" --no-headless

# Use your exact Google URL
python scraper.py --url "https://www.google.com/search?q=restaurant&udm=1&..." -m 100
```

## Output

Results are saved to the `output/` directory as timestamped CSV and/or JSON files:
- `output/results_20260312_143000.csv`
- `output/results_20260312_143000.json`

## Notes

- Google may change its page structure at any time, which can break selectors. If scraping stops working, selectors in `scraper.py` may need updating.
- Use reasonable delays and result limits to avoid being rate-limited.
- This tool is intended for personal research and lead generation purposes.
