#!/usr/bin/env python3
"""Minimal live Shopify Files media-upload verification for Product Harbor.

Uploads NEVER start from an environment flag alone. This script requires a
user-started confirmation that displays the destination shop and test scope.

Scope (hard limits):
  - exactly one extracted product
  - at most two distinct images
  - Files API only (stagedUploadsCreate + fileCreate)
  - no product create/edit/delete/publish; no sales-channel changes
  - uploaded Files are NOT deleted automatically

Usage (after connecting a store — see setup steps when disconnected):

  python scripts/verify_shopify_media_upload.py
  # prints destination + scope, then prompts:
  #   Type the destination shop hostname to start uploads:

Optional fixture (defaults to a prior Allbirds extract if present):
  --products PATH\\to\\products.json

Artifacts (no credentials written):
  artifacts/diagnostic/shopify-upload-verify/
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from shopextract.dashboard.image_assets import (  # noqa: E402
    collect_image_assets,
    download_assets,
    rewrite_products_urls,
)
from shopextract.dashboard.shopify_connection import (  # noqa: E402
    REQUIRED_SCOPES,
    SHOPIFY_API_VERSION,
    connection_path,
    load_connection,
    public_connection_status,
)
from shopextract.dashboard.shopify_csv import write_shopify_csv  # noqa: E402
from shopextract.dashboard.shopify_files import upload_assets_to_shopify  # noqa: E402

OUT = ROOT / 'artifacts' / 'diagnostic' / 'shopify-upload-verify'
DEFAULT_PRODUCTS = (
    ROOT
    / 'artifacts'
    / 'diagnostic'
    / 'browser'
    / 'zip-recheck'
    / 'jobs'
    / '1b7a88da791144628bbb7a1c3f508f1e'
    / 'products.json'
)
CONFIRM_PHRASE_HELP = 'Type the destination shop hostname exactly to start uploads'


def sanitize_cdn_url(url: str | None) -> str | None:
    """Keep scheme/host/path; drop query/fragment (may contain signed params)."""
    if not url:
        return None
    parts = urlsplit(str(url))
    if parts.scheme.lower() != 'https' or not parts.netloc:
        return 'invalid-url'
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, '', ''))


def setup_steps(artifacts_root: Path) -> str:
    path = connection_path(artifacts_root)
    scopes = ', '.join(REQUIRED_SCOPES)
    return f"""
Shopify connection is missing. Configure a throwaway/dev store first.

Exact setup steps:
  1. In Shopify Admin → Settings → Apps and sales channels → Develop apps
     → Create an app (e.g. "Product Harbor media verify").
  2. Configure Admin API scopes: {scopes}
     (Files only — do not grant product write scopes for this verification.)
  3. Install the app on the store and copy the Admin API access token.
  4. Interactive secure setup (token via getpass; never printed or passed as argv):

       cd {ROOT}
       .\\.venv\\Scripts\\python.exe scripts\\setup_shopify_connection.py

  5. Re-run dry-run / gated verify:
       .\\.venv\\Scripts\\python.exe scripts\\verify_shopify_media_upload.py --dry-run
       .\\.venv\\Scripts\\python.exe scripts\\verify_shopify_media_upload.py

Secrets file path (gitignored): {path}
API version pin: {SHOPIFY_API_VERSION}
""".strip()


def load_one_product(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding='utf-8'))
    products = payload.get('products') if isinstance(payload, dict) else payload
    if not isinstance(products, list) or not products:
        raise SystemExit(f'No products in {path}')
    product = dict(products[0])
    # Cap to at most two distinct image URLs (primary + first additional).
    primary = product.get('image_url') if isinstance(product.get('image_url'), str) else None
    extras = [u for u in (product.get('additional_images') or []) if isinstance(u, str)]
    chosen: list[str] = []
    if primary:
        chosen.append(primary)
    for url in extras:
        if url not in chosen:
            chosen.append(url)
        if len(chosen) >= 2:
            break
    if not chosen:
        raise SystemExit('Selected product has no image URLs')
    product['image_url'] = chosen[0]
    product['additional_images'] = chosen[1:]
    # Keep variants for association checks, but strip variant featured images beyond scope.
    variants = []
    for index, variant in enumerate(product.get('variants') or []):
        if not isinstance(variant, dict):
            continue
        copy = dict(variant)
        if index == 0 and len(chosen) >= 2:
            # Associate second image as featured on first variant when present.
            copy['featured_image'] = chosen[1]
        else:
            copy['featured_image'] = None
        variants.append(copy)
    product['variants'] = variants
    return product


def print_plan(shop: str, product: dict, image_urls: list[str]) -> None:
    print('=== Product Harbor — live Shopify media upload verification ===')
    print(f'Destination store : {shop}')
    print(f'API version       : {SHOPIFY_API_VERSION}')
    print(f'Required scopes   : {", ".join(REQUIRED_SCOPES)}')
    print('Test scope:')
    print(f'  - products      : 1 ({product.get("title")!r})')
    print(f'  - images        : {len(image_urls)} (hard max 2)')
    for index, url in enumerate(image_urls, start=1):
        print(f'      [{index}] {sanitize_cdn_url(url)}')
    print('  - actions       : Files staged upload + fileCreate only')
    print('  - NOT in scope  : create/edit/delete/publish products; sales channels;')
    print('                    auto-delete of uploaded Files; native CSV product import')
    print()
    print('Native Shopify CSV product-import verification: UNVERIFIED')
    print('(CSV will be generated with CDN URLs; importing it into Admin is out of scope.)')
    print()


def require_user_confirmation(shop: str) -> None:
    print(CONFIRM_PHRASE_HELP)
    print(f'  Expected: {shop}')
    try:
        typed = input('> ').strip().lower()
    except EOFError:
        raise SystemExit('Aborted: no interactive confirmation (stdin closed).')
    if typed != shop.lower():
        raise SystemExit('Aborted: confirmation did not match destination shop. No uploads started.')


async def cdn_loads(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url, headers={'Range': 'bytes=0-1023'})
        ok = response.status_code in (200, 206) and (
            (response.headers.get('content-type') or '').startswith('image/')
            or len(response.content) > 0
        )
        return {
            'status': response.status_code,
            'content_type': response.headers.get('content-type'),
            'ok': bool(ok),
            'sanitized_url': sanitize_cdn_url(url),
        }


def association_report(assets) -> list[dict]:
    rows = []
    for asset in assets:
        rows.append({
            'file_id': asset.shopify_file_id,
            'sanitized_cdn': sanitize_cdn_url(asset.shopify_cdn_url),
            'content_hash_prefix': (asset.content_hash or '')[:12] or None,
            'associations': [
                {
                    'product_handle': a.product_handle,
                    'product_title': a.product_title,
                    'role': a.role,
                    'position': a.position,
                    'variant_sku': a.variant_sku,
                    'variant_title': a.variant_title,
                }
                for a in asset.associations
            ],
        })
    return rows


def verify_csv_ordering(csv_path: Path, expected_cdns: list[str]) -> dict:
    with csv_path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    image_urls = [
        (row.get('Product image URL') or '').strip()
        for row in rows
        if (row.get('Product image URL') or '').strip()
    ]
    positions = [
        (row.get('Image position') or '').strip()
        for row in rows
        if (row.get('Product image URL') or '').strip()
    ]
    sanitized_expected = [sanitize_cdn_url(u) for u in expected_cdns]
    sanitized_actual = [sanitize_cdn_url(u) for u in image_urls]
    return {
        'csv_rows': len(rows),
        'image_rows': len(image_urls),
        'image_positions': positions,
        'sanitized_product_image_urls': sanitized_actual,
        'order_matches_upload': sanitized_actual[: len(sanitized_expected)] == sanitized_expected,
        'all_https': all((u or '').startswith('https://') for u in image_urls),
        'native_product_import': 'unverified',
    }


async def run_pass(
    *,
    conn,
    products: list[dict],
    work_dir: Path,
    mapping_path: Path,
    label: str,
) -> dict:
    assets, collect_stats = collect_image_assets(products)
    if len(assets) > 2:
        raise SystemExit(f'Internal limit breach: {len(assets)} assets (max 2)')
    images_dir = work_dir / f'images-{label}'
    images_dir.mkdir(parents=True, exist_ok=True)
    download_report = await download_assets(assets, images_dir, concurrency=2)
    downloaded = [a for a in assets if a.status == 'downloaded']
    if len(downloaded) != len(assets):
        failures = [{'url': sanitize_cdn_url(a.source_url), 'error': a.error} for a in assets if a.status != 'downloaded']
        raise SystemExit(f'Download failed before upload ({label}): {failures}')

    upload_report = await upload_assets_to_shopify(
        conn,
        assets,
        concurrency=1,
        checkpoint_path=work_dir / f'checkpoint-{label}.json',
        mapping_path=mapping_path,
    )
    cdn_checks = []
    for asset in assets:
        if not asset.shopify_cdn_url:
            cdn_checks.append({
                'file_id': asset.shopify_file_id,
                'ok': False,
                'error': asset.error or 'missing CDN URL',
            })
            continue
        check = await cdn_loads(asset.shopify_cdn_url)
        check['file_id'] = asset.shopify_file_id
        cdn_checks.append(check)

    url_map = {
        a.source_url: a.shopify_cdn_url
        for a in assets
        if a.source_url and a.shopify_cdn_url
    }
    for asset in assets:
        for alt in asset.alternate_urls:
            if asset.shopify_cdn_url:
                url_map[alt] = asset.shopify_cdn_url
    rewritten = rewrite_products_urls(products, url_map)
    csv_path = work_dir / f'products-shopify-ready-{label}.csv'
    write_shopify_csv(rewritten, csv_path)
    expected_cdns = [a.shopify_cdn_url for a in assets if a.shopify_cdn_url]
    csv_check = verify_csv_ordering(csv_path, expected_cdns)

    return {
        'label': label,
        'collect': collect_stats.as_dict(),
        'download': download_report,
        'upload': {
            'shop': upload_report.get('shop'),
            'total': upload_report.get('total'),
            'uploaded': upload_report.get('uploaded'),
            'failed': upload_report.get('failed'),
            'uploads_reused': upload_report.get('uploads_reused'),
        },
        'files': association_report(assets),
        'cdn_http_checks': cdn_checks,
        'csv_path': str(csv_path),
        'csv_checks': csv_check,
        'all_cdn_ok': all(c.get('ok') for c in cdn_checks) and bool(cdn_checks),
        'file_ids': [a.shopify_file_id for a in assets if a.shopify_file_id],
    }


async def main_async(args: argparse.Namespace) -> int:
    artifacts_root = Path(args.artifacts_root)
    status = public_connection_status(artifacts_root)
    if not status.get('connected'):
        print(setup_steps(artifacts_root))
        report = {
            'ok': False,
            'reason': 'store_configuration_missing',
            'uploads_started': False,
            'setup_required': True,
            'native_product_import': 'unverified',
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        return 2

    conn = load_connection(artifacts_root)
    assert conn is not None
    products_path = Path(args.products)
    if not products_path.is_file():
        print(f'Products fixture missing: {products_path}')
        print('Re-run a bounded dashboard extract first, or pass --products PATH')
        return 2

    product = load_one_product(products_path)
    image_urls = [product['image_url'], *product.get('additional_images', [])]
    print_plan(conn.shop, product, image_urls)

    if args.dry_run:
        print('Dry run only — no uploads started.')
        return 0

    # Env flag alone must never start uploads; interactive shop match is required.
    require_user_confirmation(conn.shop)

    OUT.mkdir(parents=True, exist_ok=True)
    work_dir = OUT / 'work'
    work_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = work_dir / f'shopify-map-{conn.shop.replace(".", "_")}.json'
    products = [product]

    print('Pass 1: upload…')
    first = await run_pass(
        conn=conn,
        products=products,
        work_dir=work_dir,
        mapping_path=mapping_path,
        label='pass1',
    )
    print(f'  uploaded={first["upload"]["uploaded"]} reused={first["upload"]["uploads_reused"]} failed={first["upload"]["failed"]}')

    print('Pass 2: same images (expect reuse, no new duplicates)…')
    # Fresh product dict; assets rebuilt; mapping file from pass1 should drive reuse.
    product2 = load_one_product(products_path)
    second = await run_pass(
        conn=conn,
        products=[product2],
        work_dir=work_dir,
        mapping_path=mapping_path,
        label='pass2',
    )
    print(f'  uploaded={second["upload"]["uploaded"]} reused={second["upload"]["uploads_reused"]} failed={second["upload"]["failed"]}')

    same_ids = sorted(first.get('file_ids') or []) == sorted(second.get('file_ids') or [])
    reuse_ok = (
        int(second['upload'].get('uploads_reused') or 0) >= len(image_urls)
        and int(second['upload'].get('failed') or 0) == 0
        and same_ids
    )

    report = {
        'ok': bool(first.get('all_cdn_ok') and second.get('all_cdn_ok') and reuse_ok),
        'destination_shop': conn.shop,
        'api_version': conn.api_version,
        'uploads_started': True,
        'scope': {
            'products': 1,
            'images': len(image_urls),
            'title': product.get('title'),
        },
        'pass1': first,
        'pass2': second,
        'reuse': {
            'pass2_uploads_reused': second['upload'].get('uploads_reused'),
            'file_ids_unchanged': same_ids,
            'ok': reuse_ok,
        },
        'files_not_deleted': True,
        'native_product_import': 'unverified',
        'note': (
            'Uploaded Files remain in the store Files library. '
            'Native Shopify Admin CSV product import was not executed.'
        ),
    }
    report_path = OUT / 'report.json'
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print()
    print(f'Report written: {report_path}')
    print(f'ok={report["ok"]} reuse_ok={reuse_ok} native_product_import=unverified')
    if not report['ok']:
        print('See report.json for file IDs, sanitized CDN URLs, and errors.')
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--artifacts-root',
        default=str(ROOT / 'artifacts'),
        help='Root that holds secrets/shopify-connection.json (default: ./artifacts)',
    )
    parser.add_argument(
        '--products',
        default=str(DEFAULT_PRODUCTS),
        help='products.json from a prior extract (first product used; images capped at 2)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print destination + scope only; never upload',
    )
    return parser


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
