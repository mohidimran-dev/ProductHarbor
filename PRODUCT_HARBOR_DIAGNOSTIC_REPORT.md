# Product Harbor — Diagnostic Report

**Date:** 2026-11-06  
**Scope:** Read-only audit of local code + existing tests + one bounded live Allbirds run.  
**No application code was changed.** Temporary artifacts under `artifacts/diagnostic/`.  
**No Shopify live uploads. No full-catalog crawl.**

---

## 1. Project overview

### Git

| Item | Value |
|------|--------|
| Branch | `main` |
| HEAD commit | `8e67b014eecc2467526280e32df7fc345a10e0b3` |
| HEAD subject | Release ProductHarbor workspace with dashboard, interactive browser, and Shopify CSV export. |
| Working tree | **Dirty** — media/dedupe/Shopify-ready work is present but **not committed** |

**Change summary (uncommitted):**

- Modified: `.gitignore`, `pyproject.toml` (+Pillow), `_execution.py`, `_extract.py`, `extractors/shopify.py`, `extractors/unified.py`, dashboard `app.py` / `jobs.py` / `shopify_csv.py` / static UI, `__main__.py`
- Untracked: `_dedupe.py`, `dashboard/image_assets.py`, `media_jobs.py`, `shopify_connection.py`, `shopify_files.py`, `zip_export.py`, related tests

### Runtime / frameworks

| Component | Observed |
|-----------|----------|
| Package name (internal) | `shopextract` 0.1.2 (`pyproject.toml`) |
| Display name | Product Harbor |
| Python | 3.14.10 (venv) |
| Backend HTTP | `aiohttp` 3.14.5 (**installed**, but **not declared** in `pyproject.toml` dependencies) |
| HTTP client | `httpx` 0.28.1 |
| HTML parse | `beautifulsoup4` 4.15.0 |
| Crawl | `crawl4ai` (installed) |
| Images | `Pillow` 13.0.0 (declared in dirty `pyproject.toml`) |
| Frontend | Static HTML/CSS/JS served by aiohttp (`dashboard/static/`) — no React/Vue |

### Windows startup commands

```powershell
cd D:\Scraper\shopextract
.\.venv\Scripts\Activate.ps1
python -m shopextract.dashboard
# → http://127.0.0.1:8765/
```

CLI extract (used for live diagnostic):

```powershell
cd D:\Scraper\shopextract
.\.venv\Scripts\python.exe -c "import asyncio; from shopextract import extract; ..."
```

### Main files (by concern)

| Concern | Primary files / symbols |
|---------|-------------------------|
| Orchestration | `src/shopextract/_extract.py` (`extract`, `_normalize_batch`, `_extract_interactive_collection`) |
| Platform detection | `src/shopextract/_detect.py` |
| Shopify API scrape / pagination | `src/shopextract/extractors/shopify.py` (`ShopifyExtractor.extract` → `{base}/products.json?page=`) |
| Interactive / custom | `src/shopextract/extractors/_interactive_browser.py` |
| Unified crawl | `src/shopextract/extractors/unified.py` |
| Normalization | `src/shopextract/_normalize.py` (`normalize`, `_normalize_shopify`) |
| Identity | `src/shopextract/identity.py` (`assign_identity`) |
| Product dedupe | `src/shopextract/_dedupe.py` (**untracked**) |
| Access blocked | `src/shopextract/_access.py`; handled in `_extract.py`, `dashboard/jobs.py` |
| Checkpoints | `src/shopextract/_execution.py` (`ProgressCheckpoint`) |
| Image download / dedupe | `src/shopextract/dashboard/image_assets.py` (**untracked**) |
| ZIP package | `src/shopextract/dashboard/zip_export.py` (**untracked**) |
| Shopify Files upload | `src/shopextract/dashboard/shopify_files.py`, `shopify_connection.py` (**untracked**) |
| Media jobs | `src/shopextract/dashboard/media_jobs.py` (**untracked**) |
| Shopify CSV | `src/shopextract/dashboard/shopify_csv.py` |
| Dashboard API / UI | `dashboard/app.py`, `jobs.py`, `static/index.html`, `dashboard.js`, `dashboard.css` |

### Capability status (evidence-based)

| Capability | Status | Evidence |
|------------|--------|----------|
| Shopify collection API extract | **Implemented** | Live Allbirds run; `ShopifyExtractor`; collection `…/products.json` |
| Platform detection | **Implemented** | `_detect.py`; live `platform=shopify` |
| Pagination / budget stop | **Implemented** | Live stop `product_budget_reached`; `shopify.py` paging |
| Normalization (title/price/images/variants) | **Implemented** | Live field match vs collection JSON; `_normalize.py` |
| Product dedupe | **Implemented (uncommitted)** | `_dedupe.py`; tests pass; regresses one checkpoint test |
| Image download + byte/pixel dedupe | **Implemented (uncommitted)** | Live 10/10 downloads; `image_assets.py` |
| CSV + Images ZIP | **Implemented (uncommitted)** | ZIP listing verified under `artifacts/diagnostic/` |
| Shopify staged upload → CDN CSV | **Implemented in code + mocked tests; live upload unverified** | `shopify_files.py`; `test_media_zip_and_shopify_ready.py` (mocked) |
| Shopify CSV export | **Implemented** | Live CSV 85 rows; `shopify_csv.py` |
| Dashboard extract/progress/downloads | **API verified (mocked); browser UI unverified** | `dashboard-api-checks.json` |
| ACCESS_BLOCKED vs empty | **Implemented + tested** | `test_access_blocked.py`, dashboard blocked/empty states |
| Cancel / resume (extract + media) | **Partial** | Extract cancel API verified; media cancel/resume mocked tests pass; live resume unverified |
| Option name mapping (Size vs option1) | **Partial / weak** | Live CSV `Option1 name=option1` (`_normalize_shopify` uses `option{i}` keys) |
| Full collection completeness proof | **Not claimed at max=5** | Budget stop is intentional partial |
| Live Shopify media upload | **Unverified** | Not run (policy) |

---

## 2. Existing tests

### Command

```powershell
cd D:\Scraper\shopextract
.\.venv\Scripts\python.exe -m pytest `
  tests/test_product_image_dedupe.py `
  tests/test_image_assets.py `
  tests/test_media_zip_and_shopify_ready.py `
  tests/test_media_cancel_resume.py `
  tests/test_shopify_csv_export.py `
  tests/test_dashboard_jobs.py `
  tests/test_access_blocked.py `
  tests/test_interactive_browser.py `
  tests/test_normalize.py `
  tests/test_extract.py `
  tests/test_detect.py `
  tests/test_shopify_collection_smoke.py `
  tests/test_truthful_outcomes.py `
  tests/test_runtime_checkpoint.py `
  -q --tb=line
```

### Result

| Metric | Count |
|--------|-------|
| **Passed** | **203** |
| **Failed** | **1** |
| **Skipped** | **0** (none reported) |
| Duration | ~86s |

**Mocked vs live:** All of the above are **mocked / unit / fixture** tests (httpx MockTransport, fake extractors, etc.).  
`test_shopify_collection_smoke.py` uses a mocked `products.json`, not a live store.

### Failure

`tests/test_runtime_checkpoint.py::test_checkpoint_reuses_codec_budget_boundaries[nodes-0]`

- Asserts `len(result.products) == 2` when registering `[product]*2` with the **same** `external_id`.
- After uncommitted `dedupe_products` in `ProgressCheckpoint.snapshot()` (`_execution.py`), identical IDs collapse to **1** product.
- **Expected by test:** two retained payload slots for budget math.  
- **Actual:** one unique product.  
- Not counted as a pass.

---

## 3. Live extraction (Allbirds, max 5)

**URL:** `https://www.allbirds.com/collections/womens`  
**Artifact:** `artifacts/diagnostic/allbirds-live-5.json`

| Field | Observed |
|-------|----------|
| Platform | `shopify` |
| Method / tier | `api` (`/collections/womens/products.json` path via extractor base URL) |
| Elapsed | **7.29s** |
| `product_count` | **5** |
| `variant_count` | **65** |
| Image references | **25** total / **25** unique |
| Duplicate external IDs | none |
| Duplicate product URLs | none |
| `catalog_complete` | `false` |
| Outcome | `partial` |
| Stop reason | `product_budget_reached` |
| `observation_scope` | `shopify:catalog:https://www.allbirds.com/collections/womens` |
| `urls_attempted` / `urls_succeeded` | 1 / 1 |

### Counter meanings (code)

| Counter | Meaning in this run |
|---------|---------------------|
| `urls_attempted` / `urls_succeeded` | API path treats collection fetch as one attempt (`_extract.py` API success branch) |
| Dashboard `discovered` | `max(urls_attempted, product_count)` (`jobs.py`) |
| Dashboard `extracted` | `result.product_count` |
| Dashboard `failed` | `max(0, discovered - extracted)` when `urls_attempted` set |
| Live CLI `product_count` | Normalized unique products after dedupe |

**Completeness:** A five-product limit is **partial by design**, not proof the women’s collection is fully enumerated.

### Collection scope evidence (not URL-path-only)

- Collection endpoint `https://www.allbirds.com/collections/womens/products.json?limit=5` returned the **same three IDs/titles** as the extract.
- Site-wide `https://www.allbirds.com/products.json?limit=250` did **not** return those IDs in the first page used for the failed initial compare — confirming collection-scoped API membership matters.
- Response header `powered-by: Shopify` observed on collection JSON.

### Source compare (3 products vs collection `products.json`)

Artifact: `artifacts/diagnostic/allbirds-field-diff.json`

| Check | Result |
|-------|--------|
| Title / price / variant count | Match |
| Gallery count (5) + ordered URLs | Match |
| Currency | `USD` present |
| Description | Source HTML length 188 vs extracted text length 181 (HTML stripped — expected difference, not invented content) |
| Variant `featured_image` | Source and extract both **0** URLs for these size variants |
| Option labels | Extracted as `option1` keys (not human “Size”) |

---

## 4. Image and export check

### Shopify CSV (local)

- File: `artifacts/diagnostic/allbirds-products-shopify.csv`
- Rows: **85** (5 products × variants + extra gallery image rows)
- Handles: 5 distinct product handles
- Product image URL cells: 25 (gallery positions)
- Variant image URL filled: **0** (matches source: no explicit variant images)
- Local / `file:` / `data:` URLs: **none**
- Sample issues: `Option1 name` = `option1` (not Size); repeated handles on variant/image rows are **valid** Shopify CSV shape

### Image download (≤10 refs)

- Requested unique refs: 10  
- Downloaded: **10** / failed: **0**  
- Unique SHA-256 prefixes: **10** (no accidental byte collapse)  
- Artifact: `artifacts/diagnostic/allbirds-image-download.json`  
- Files under `artifacts/diagnostic/images/` (large PNGs; original bytes preserved)

### ZIP package

- File: `artifacts/diagnostic/allbirds-csv-images-BACKUP.zip`
- Contents observed: `products.csv`, `image-manifest.json`, `download-report.json`, `README-BACKUP.txt`, `images/*` (10 files)
- Labeled backup package in README (native Shopify importer still needs HTTPS URLs)

### Shopify upload readiness

| Item | Status |
|------|--------|
| Connection storage server-side | Code present (`artifacts/secrets/…`); token not returned by `/api/shopify/status` (**API check passed**) |
| GraphQL staged upload + `fileCreate` | Code present; **mocked** tests only |
| Live upload to a store | **Not performed** |
| Required scopes (docs/code) | `write_files`, `read_files`; API version `2025-10` in `shopify_connection.py` |

---

## 5. Dashboard check

| Check | Method | Result |
|-------|--------|--------|
| Health | aiohttp TestClient | Pass |
| Shopify status hides token | TestClient | Pass |
| Succeeded → CSV download 200 | TestClient + mocked extract | Pass |
| Blocked → no CSV (404) | TestClient | Pass |
| Empty → failed, no CSV | TestClient | Pass |
| Partial → CSV 200 | TestClient | Pass |
| Cancel → `cancelled` | TestClient | Pass |
| Browser UI (start button, live paint, visual progress) | — | **Unverified** (no browser automation in this audit) |
| Real progress during live Allbirds via UI | — | **Unverified** |

Artifact: `artifacts/diagnostic/dashboard-api-checks.json`

### Safety notes (code inspection)

- Status/reason/title rendering uses `textContent` (not HTML injection of descriptions).
- Preview sets `img.src = item.image_url` from scraped data (`dashboard.js`) — **URL-based image load**, not HTML render of descriptions; still trust-boundary for malicious image URLs.
- Shopify access token: saved server-side; connect response uses `public_status` without token (**verified** via API). Not grepped into CSV/ZIP in this run.

---

## 6. Top risks (user impact order)

### 1. Uncommitted media/dedupe stack + packaging gap

- **Evidence:** `git status` shows core media/dedupe modules untracked; `aiohttp` required by dashboard but missing from `pyproject.toml`.
- **Expected:** Reproducible install + committed feature set for another developer.
- **Actual:** Features work in this dirty workspace; clean clone may miss deps/files.
- **Fix:** Commit intentional modules; declare `aiohttp` (and confirm Playwright if interactive browser is required).
- **Effort:** Small

### 2. Checkpoint budget test / dedupe interaction

- **Evidence:** `test_runtime_checkpoint.py` failure; `dedupe_products` in `ProgressCheckpoint.snapshot()`.
- **Expected:** Checkpoint budget accounting retains configured payload slots.
- **Actual:** Same-ID duplicates merge → assertion fails.
- **Fix:** Dedupe after budget serialization, or use identity-aware fixtures; don’t merge synthetic checkpoint duplicates used only for byte/node budgets.
- **Effort:** Small–medium

### 3. Shopify option labels exported as `option1` / `option2`

- **Evidence:** Live CSV sample; `_normalize.py` Shopify variant path sets `attributes[f'option{i}']`.
- **Expected:** Human option names (e.g. Size) from Shopify `options` array.
- **Actual:** Generic `option1` names in CSV → worse merchant import UX.
- **Fix:** Map Shopify product `options[].name` onto variant attributes during normalize / CSV write.
- **Effort:** Medium

### 4. Live Shopify-ready upload path unproven

- **Evidence:** Only mocked `upload_assets_to_shopify` in tests; no live Files API run.
- **Expected:** Operator confidence that CDN remap works against a real shop.
- **Actual:** Code + mocks only.
- **Fix:** Optional gated integration test with a throwaway shop; keep secrets out of CI logs.
- **Effort:** Medium

### 5. Dashboard browser UX and progress semantics unverified end-to-end

- **Evidence:** API checks only; `urls_attempted=1` on successful Shopify API runs makes “discovered” ≈ product count, which can confuse operators during API extracts.
- **Expected:** UI progress clearly means products vs HTTP pages; cancel/resume visible for media jobs in a real browser.
- **Actual:** Not browser-verified; counter semantics are easy to misread.
- **Fix:** Document counters in UI; add Playwright smoke for workspace buttons.
- **Effort:** Medium

---

## Sanitized sample product

From live extract (Allbirds women’s, ID `7214902247504`):

- **Title:** Women's Wool Runner NZ Waterproof - Dark Navy (Natural White Sole)
- **Price / currency:** 150.00 USD
- **Variants:** 13 (sizes 5–11); no explicit variant image URLs in source
- **Gallery:** 5 Shopify CDN HTTPS URLs (ordered match to collection JSON)
- **Product URL:** `https://www.allbirds.com/collections/womens/products/womens-wool-runner-nz-waterproof-dark-navy`

### Relevant CSV rows (sanitized)

| Title | Handle | SKU | Price | Product image URL | Image position | Variant image | Option1 name | Option1 value |
|-------|--------|-----|-------|-------------------|----------------|---------------|--------------|---------------|
| Women's Wool Runner…Dark Navy… | women-s-wool-runner-nz-waterproof-dark-navy-natural-white-sole | A11987W050 | 150.00 | https://cdn.shopify.com/…LEFT….png?v=… | 1 | (blank) | option1 | 5 |
| (blank) | same handle | A11987W055 | 150.00 | (blank) | (blank) | (blank) | option1 | 5.5 |
| … | … | … | … | additional gallery rows positions 2–5 | … | … | … | … |

---

## Outstanding limitations

- Dirty git tree: media/dedupe/Shopify-ready not on HEAD commit.
- `aiohttp` undeclared in packaging metadata.
- One checkpoint regression with product dedupe.
- Option names not merchant-friendly.
- Live CDN upload path not exercised.
- Browser UI not audited.
- max_urls=5 cannot certify full collection completeness.
- Interactive browser / ACCESS_BLOCKED live Allbirds path not exercised (API succeeded).

## Recommended next steps

1. Commit or explicitly branch the media/dedupe work; add missing package deps.  
2. Fix checkpoint↔dedupe interaction and restore green `test_runtime_checkpoint`.  
3. Map Shopify option names into CSV.  
4. Add a gated Shopify Files upload smoke (non-CI or secret-backed).  
5. Add a thin Playwright dashboard smoke + clearer counter copy.

---

## Artifacts produced (local)

- `artifacts/diagnostic/allbirds-live-5.json`
- `artifacts/diagnostic/allbirds-field-diff.json`
- `artifacts/diagnostic/allbirds-source-endpoints.json`
- `artifacts/diagnostic/allbirds-products-shopify.csv`
- `artifacts/diagnostic/allbirds-csv-sample-rows.json`
- `artifacts/diagnostic/allbirds-image-download.json`
- `artifacts/diagnostic/allbirds-csv-images-BACKUP.zip`
- `artifacts/diagnostic/dashboard-api-checks.json`
- `artifacts/diagnostic-pytest.txt` (if written)
