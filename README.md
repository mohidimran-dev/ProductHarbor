# ProductHarbor

**Extract public storefront collections into clean product data — with live progress, evidence-backed completeness, and Shopify-ready export.**

ProductHarbor is a local extraction workspace for public e-commerce collections. Point it at a collection URL, watch discovery and product admission in the dashboard, and download JSON, a run report, or a Shopify Admin–compatible CSV.

---

## Highlights

- **Platform-aware extraction** — Auto-detect Shopify, WooCommerce, or run a custom crawl path
- **Interactive headed browser** — When access gates appear, clear challenges yourself in a persistent Chromium profile (no CAPTCHA bypass)
- **Live preview** — Job status, browser snapshots, and product cards update as evidence arrives
- **Honest completeness** — Blocked runs stay `ACCESS_BLOCKED` with the real response; no fake success
- **Shopify CSV export** — Download scraped products as a template-compatible Admin import CSV

---

## Requirements

- Python 3.10+
- Windows / macOS / Linux
- Playwright Chromium (for browser / interactive mode)

---

## Setup

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
python -m playwright install chromium
```

---

## Dashboard (recommended)

Start the local ProductHarbor workspace:

```bash
python -m shopextract.dashboard --host 127.0.0.1 --port 8765
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

1. Choose **Auto Detect**, **Shopify**, **WooCommerce**, or **Custom / Other**
2. Paste a **collection URL**
3. Pick **Test (5)** or **Full (up to 1000)**
4. For protected storefronts, enable **Interactive headed browser**, clear any challenge yourself, then click **Continue**
5. Download:
   - **Download JSON** — product payload
   - **Download run report** — job evidence
   - **Download for Shopify** — Admin product CSV (template-compatible)

The dashboard binds to localhost by default.

---

## CLI

```bash
# Extract products
shopextract extract "https://example.com/collections/all" -n 20 -o products.json

# Interactive browser (human clears challenges; implies --browser)
shopextract extract "https://example.com/collections/all" -n 5 --interactive-browser --timeout 300

# Detect platform
shopextract detect "https://example.com"
```

---

## Python API

```python
import asyncio
from shopextract import extract
from shopextract._execution import ExecutionPolicy, execution_policy

async def main():
    with execution_policy(ExecutionPolicy(browser=True, interactive_browser=True), 300):
        result = await extract(
            "https://example.com/collections/all",
            max_urls=5,
        )
    for product in result.products:
        print(product.title, product.price, product.currency)

asyncio.run(main())
```

---

## Shopify CSV mapping

`Download for Shopify` uses the Shopify Admin product CSV column set from the official product template, including:

| Source field | CSV column |
|---|---|
| Title | `Title` |
| Slug from title | `URL handle` |
| Description | `Description` |
| SKU | `SKU` |
| Price | `Price` |
| Image URL | `Product image URL` |
| Variant options | `Option1/2/3 name` + `value` |
| Variant image | `Variant image URL` |

Fields that were not published by the source are left blank — ProductHarbor does not invent values.

---

## Project layout

```
src/shopextract/
  dashboard/           # ProductHarbor local UI + job runner
  extractors/          # Platform + interactive browser extractors
  ...
tests/                 # Unit and dashboard tests
```

The installable Python package module remains `shopextract` for compatibility; the product brand is **ProductHarbor**.

---

## Tests

```bash
pytest tests/test_dashboard_jobs.py tests/test_interactive_browser.py tests/test_shopify_csv_export.py tests/test_access_blocked.py -q
```

---

## Security notes

- ProductHarbor does **not** bypass CAPTCHAs, logins, or bot protections
- Interactive mode waits for a human Continue signal only
- Do not commit `.env`, credentials, or `artifacts/` scrape dumps

---

## License

MIT License. See [LICENSE](LICENSE).
