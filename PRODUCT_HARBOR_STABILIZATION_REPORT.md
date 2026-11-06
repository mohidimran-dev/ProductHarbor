# Product Harbor — Stabilization Report

**Date:** 2026-11-06  
**Basis:** `PRODUCT_HARBOR_DIAGNOSTIC_REPORT.md`  
**Scope:** Implement fixes; preserve working features and unrelated dirty-tree work; no commit/push; no live Shopify upload.

---

## 1. Fixes implemented

### 1.1 Reproducible installation / packaging

| Change | Detail |
|--------|--------|
| Declared `aiohttp>=3.9` | Direct dashboard runtime dependency in `pyproject.toml` |
| Declared `Pillow>=10.0` | Image byte/pixel dedupe + ZIP packaging |
| Optional `browser` extra | `playwright>=1.40` for headed Custom-tab browser |
| Wheel `force-include` | `src/shopextract/dashboard/static` → `shopextract/dashboard/static` |
| New modules | Packaged via hatch `packages = ["src/shopextract"]` (`_dedupe`, `image_assets`, `media_jobs`, `shopify_connection`, `shopify_files`, `zip_export`) |

**Taxonomy bootstrap (reproducibility):** Runtime wheels expect the pinned compact catalog; cold Git checkouts still need explicit prepare via `scripts/bootstrap_taxonomy.py` / `taxonomy-build` (PyYAML) as documented in `docs/shopify-taxonomy.md`. Ordinary `pip install -e ".[dev]"` does **not** require PyYAML for dashboard extract.

### 1.2 Checkpoint / dedupe contract

**Intended contract:** `ProgressCheckpoint` admits products under codec/node/byte/depth budgets **before** same-identity merge. Product dedupe by `(platform, store, id)` / canonical URL remains mandatory after snapshot.

| Fix | Evidence |
|-----|----------|
| Codec-budget fixture uses distinct `external_id` (`P1`, `P2`) | `tests/test_runtime_checkpoint.py::test_checkpoint_reuses_codec_budget_boundaries` |
| Same-ID merge coverage | `test_checkpoint_same_id_observations_merge_after_budget_accept` |
| Distinct-ID retention after round-trip | `test_checkpoint_distinct_ids_retained_after_round_trip` |
| Did **not** weaken dedupe or change expected 2→1 | Production dedupe preserved |

### 1.3 Source option names

| Fix | Files |
|-----|-------|
| Preserve Shopify `options[].name` + position → `Product.option_names` | `_normalize.py`, `_models.py`, `_portable.py` |
| Map `option1/2/3` values onto source labels; never invent labels | `_normalize.py` |
| CSV uses source labels when variant carries those keys | `dashboard/shopify_csv.py` |
| Tests: multi-option, missing labels, duplicate labels | `tests/test_shopify_options_and_description.py` |

Live Allbirds sample: `option_names=['Size']`; CSV `Option1 name=Size` / `Option1 value=5`.

### 1.4 Truthful dashboard counters

| Fix | Detail |
|-----|--------|
| Removed inferred discovered/failed from `max(urls, products)` / arithmetic | `dashboard/jobs.py` |
| Separate measured fields: http_pages, discovered, extracted, failed, duplicates_merged, pending | `Job.public()` + UI |
| UI shows `—` for null/unknown | `dashboard.js` `fmt()` |
| No numeric completion % without known denominator | Not rendered |
| Test/5-product and budget-stop runs marked limited/partial | `limited_run` + `#run-limit-note` |
| Completeness = confirmed only when `catalog_complete is True` | Succeeded path |
| Mid-run progress no longer treats URL attempts as discoveries | `_progress_counts` / `_poll_progress` |

### 1.5 Description preservation

| Fix | Detail |
|-----|--------|
| Retain source `body_html` as `description_html` | `_normalize.py` |
| Separate plain `description` | model + export JSON |
| Shopify CSV `Description` column prefers HTML (Shopify product template field; supports `<p>` / lists) | `shopify_csv.py` |
| Dashboard preview does not inject HTML descriptions (title/price only → safe) | `dashboard.js` |

Live Allbirds: `description_html` with `<p>…</p>`; CSV `Description` contains same HTML.

### 1.6 Shopify upload status

| Item | Status |
|------|--------|
| Live Files upload | **Unverified** (not executed; policy) |
| Configured API version | Was `2025-10` → **updated to `2026-10`** |
| Official docs | https://shopify.dev/docs/api/usage/versioning — as of 2026-11-06, `2025-10` is inaccessible (retired 2026-10-16 15:00 UTC; Shopify falls forward). Latest stable listed includes `2026-10`. |
| Auth | Still requires custom-app Admin token with `write_files` + `read_files`; not live-tested |
| Prepared integration test | `tests/test_shopify_upload_integration.py` — skipped unless `PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD=1` + shop/token |

---

## 2. Clean-install result

**Preserved working `.venv`.** Separate env: `artifacts/diagnostic/clean-venv`.

```powershell
cd D:\Scraper\shopextract
py -3.14 -m venv artifacts\diagnostic\clean-venv
.\artifacts\diagnostic\clean-venv\Scripts\python.exe -m pip install -U pip
.\artifacts\diagnostic\clean-venv\Scripts\pip.exe install -e ".[dev]"
```

| Check | Result |
|-------|--------|
| `pip install -e ".[dev]"` | **Succeeded** (exit 0) |
| `import aiohttp` | 3.14.5 |
| `shopextract.dashboard` static `index.html` | **exists** |
| `shopextract._dedupe` | importable |
| Dashboard HTTP smoke (`create_app` → GET `/`) | **200**, ProductHarbor HTML present |
| Log files | `artifacts/diagnostic/clean-install.log`, `clean-import.txt`, `clean-dashboard-smoke.txt` |

No installation failure to report for the clean editable+dev path on this machine (Python 3.14.10 / Windows).

---

## 3. Automated test results

### Command (exact)

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
  tests/test_shopify_options_and_description.py `
  tests/test_shopify_upload_integration.py `
  -q --tb=line
```

### Counts

| Passed | Failed | Skipped |
|--------|--------|---------|
| **210** | **0** | **2** |

Skipped: live Shopify upload integration (env gate).  
Log: `artifacts/diagnostic/stabilization-pytest.txt`.

**Not claimed:** full repository pytest suite beyond this audited+new set.

---

## 4. Browser verification results

Harness: `artifacts/diagnostic/browser_dashboard_check.py`  
Results: `artifacts/diagnostic/browser/browser-results.json`  
Screenshots: `artifacts/diagnostic/browser/screenshots/`

| Check | Result |
|-------|--------|
| Fixture succeeded + downloads JSON/report/Shopify CSV | **Pass** |
| Blocked | **Pass** (`blocked`, downloads disabled) |
| Empty → failed | **Pass** |
| Partial / limited note | **Pass** |
| Cancel | **Pass** (`cancelled`) |
| Live Allbirds ≤5 | **Pass** — state `partial`, extracted `5`, limited note on, completeness incomplete |
| Live JSON / report / Shopify CSV download | **Pass** |
| Live CSV + Images ZIP | **Pass on dedicated recheck** (25/25 images, ZIP ~114 MB). First Playwright pass exited early on progress text containing “failed”; API recheck waited for terminal media state — see `zip-recheck/` |

### Unverified (explicit)

- Live Shopify Files upload / Shopify-ready CDN CSV
- Interactive headed Continue against a real CAPTCHA/login gate
- Full-collection Allbirds exhaustion
- Media **Resume** after cancel in the live browser (unit tests cover cancel/resume; browser resume not exercised)

---

## 5. Remaining limitations

1. Live Shopify media upload remains **unverified** until a throwaway shop runs `PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD=1`.
2. API pin moved to `2026-10`; mutation compatibility with Files upload still needs that live smoke.
3. Some mid-run counters stay `—` until finalize measures them (intentional honesty).
4. Image CDN downloads can flake under network cutoffs; ZIP job can finish partial — retry/resume path exists in media jobs.
5. Taxonomy cold-checkout still needs explicit bootstrap for taxonomy features (not required for basic dashboard Shopify extract).

---

## 6. Windows startup commands

```powershell
cd D:\Scraper\shopextract
.\.venv\Scripts\Activate.ps1
python -m shopextract.dashboard
# → http://127.0.0.1:8765/
```

Fresh install (optional; does not replace `.venv`):

```powershell
cd D:\Scraper\shopextract
py -3.14 -m venv .venv-fresh
.\.venv-fresh\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[dev]"
# Optional headed browser:
pip install -e ".[browser]"
python -m playwright install chromium
python -m shopextract.dashboard
```

Enable later Shopify upload smoke (user-started only):

```powershell
$env:PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD='1'
$env:PRODUCTHARBOR_SHOPIFY_SHOP='your-store.myshopify.com'
$env:PRODUCTHARBOR_SHOPIFY_ADMIN_TOKEN='shpat_...'
.\.venv\Scripts\python.exe -m pytest tests/test_shopify_upload_integration.py -q
```

---

## 7. Intended source/test file list (later commit — not committed)

**Do not commit:** credentials, `artifacts/`, browser profiles, downloaded images, clean-venv.

### Modified

- `.gitignore`
- `pyproject.toml`
- `src/shopextract/_execution.py`
- `src/shopextract/_extract.py`
- `src/shopextract/_models.py`
- `src/shopextract/_normalize.py`
- `src/shopextract/_portable.py`
- `src/shopextract/dashboard/__main__.py`
- `src/shopextract/dashboard/app.py`
- `src/shopextract/dashboard/jobs.py`
- `src/shopextract/dashboard/shopify_csv.py`
- `src/shopextract/dashboard/static/dashboard.css`
- `src/shopextract/dashboard/static/dashboard.js`
- `src/shopextract/dashboard/static/index.html`
- `src/shopextract/extractors/shopify.py`
- `src/shopextract/extractors/unified.py`
- `tests/test_runtime_checkpoint.py`

### New (untracked)

- `src/shopextract/_dedupe.py`
- `src/shopextract/dashboard/image_assets.py`
- `src/shopextract/dashboard/media_jobs.py`
- `src/shopextract/dashboard/shopify_connection.py`
- `src/shopextract/dashboard/shopify_files.py`
- `src/shopextract/dashboard/zip_export.py`
- `tests/test_image_assets.py`
- `tests/test_media_cancel_resume.py`
- `tests/test_media_zip_and_shopify_ready.py`
- `tests/test_product_image_dedupe.py`
- `tests/test_shopify_options_and_description.py`
- `tests/test_shopify_upload_integration.py`
- `PRODUCT_HARBOR_DIAGNOSTIC_REPORT.md`
- `PRODUCT_HARBOR_STABILIZATION_REPORT.md`

---

## 8. ChatGPT paste summary

```
Product Harbor stabilization (2026-11-06) — DONE, not committed.

INSTALL: aiohttp+Pillow declared; static force-included; clean-venv pip install -e ".[dev]" OK; dashboard GET / 200. Working .venv preserved.

CHECKPOINT/DEDUPE: codec-budget fixture now uses distinct product IDs; added same-ID merge + distinct-ID retention tests; dedupe not weakened. nodes-0 case fixed properly.

OPTIONS: Shopify options[].name → option_names; variant option1/2/3 mapped to source labels; CSV Option1 name=Size on live Allbirds; no invented labels.

COUNTERS: no inferred discovered/failed; null→"—" in UI; limited_run for test/budget; completeness only when catalog_complete True.

DESCRIPTIONS: description_html + plain description retained; CSV Description carries source HTML <p>; preview does not XSS-inject HTML.

BROWSER: fixture succeeded/blocked/empty/partial/cancel OK; live Allbirds ≤5 partial+downloads OK; ZIP OK on recheck (25/25, ~114MB). Unverified: live Shopify upload, interactive CAPTCHA continue, full catalog.

SHOPIFY UPLOAD: still UNVERIFIED. API version bumped 2025-10→2026-10 (2025-10 retired 2026-10-16 per Shopify docs). Integration test gated on PRODUCTHARBOR_SHOPIFY_LIVE_UPLOAD=1.

TESTS: 210 passed, 0 failed, 2 skipped (audited suite + options/upload gate). Not full-repo.

START: cd D:\Scraper\shopextract; .\.venv\Scripts\Activate.ps1; python -m shopextract.dashboard → http://127.0.0.1:8765/
```
