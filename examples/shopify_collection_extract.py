"""Full Shopify collection validation (no artificial product budget).

Uses the public collection products.json API. Supported extraction budget is
max_urls (positive int); CLI caps at 1000. This run uses 1000 so a typical
collection can exhaust naturally (short final page / empty page).

Usage:
    .\\.venv\\Scripts\\python.exe examples\\shopify_collection_extract.py
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import shopextract
from shopextract._models import Platform, Product
from shopextract.extractors._http_defaults import DEFAULT_HEADERS, get_default_user_agent
from shopextract.extractors.shopify import ShopifyExtractor

COLLECTION_URL = 'https://www.allbirds.com/collections/womens'
SHOP_ORIGIN = 'https://www.allbirds.com'
# Supported configuration: raise above the former smoke budget of 5 so pagination
# can reach a confirmed end (CLI-compatible upper bound is 1000).
MAX_PRODUCTS = 1000
OUT_DIR = Path('artifacts/shopify-collection-validation')
PRODUCTS_JSON = OUT_DIR / 'allbirds-womens-products.json'
REPORT_JSON = OUT_DIR / 'allbirds-womens-run-report.json'


def _money(value):
    if value is None or value == '':
        return None
    return str(Decimal(str(value)))


def _strip_html(value: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', value or '')
    return re.sub(r'\s+', ' ', text).strip()


def product_record(product: Product) -> dict:
    return {
        'external_id': product.external_id or None,
        'canonical_product_id': product.canonical_product_id or None,
        'title': product.title or None,
        'description': product.description or None,
        'price': _money(product.price) if product.price is not None else None,
        'compare_at_price': _money(product.compare_at_price),
        'currency': product.currency or None,
        'sku': product.sku,
        'image_url': product.image_url or None,
        'additional_images': list(product.additional_images or []),
        'product_url': product.product_url or None,
        'vendor': product.vendor,
        'product_type': product.product_type,
        'in_stock': product.in_stock,
        'tags': list(product.tags or []),
        'variants': [
            {
                'variant_id': v.variant_id or None,
                'canonical_variant_id': v.canonical_variant_id or None,
                'title': v.title or None,
                'price': _money(v.price) if v.price is not None else None,
                'sku': v.sku,
                'gtin': v.gtin,
                'in_stock': v.in_stock,
                'options': dict(v.attributes or {}),
                'featured_image': v.featured_image,
                'featured_image_id': v.featured_image_id,
            }
            for v in (product.variants or [])
        ],
    }


async def fetch_source_catalog(collection_url: str) -> dict:
    headers = {**DEFAULT_HEADERS, 'User-Agent': get_default_user_agent(), 'Accept': 'application/json'}
    products = []
    pages = 0
    ended = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0, headers=headers) as client:
        page = 1
        while page <= 100:
            response = await client.get(f'{collection_url.rstrip("/")}/products.json',
                                        params={'limit': 250, 'page': page})
            if response.status_code != 200:
                ended = f'http_{response.status_code}'
                break
            batch = response.json().get('products') or []
            pages += 1
            if not batch:
                ended = 'empty_page'
                break
            products.extend(batch)
            if len(batch) < 250:
                ended = 'short_final_page'
                break
            page += 1
        else:
            ended = 'page_budget_reached'
    return {
        'products': products,
        'pages_completed': pages,
        'end_reason': ended,
        'product_ids': [str(p.get('id')) for p in products if p.get('id') is not None],
    }


def assert_collection_scope(products: list[Product], collection_url: str) -> dict:
    collection_path = urlsplit(collection_url).path.rstrip('/')
    in_scope = []
    out_of_scope = []
    for product in products:
        url = product.product_url or ''
        raw_handle = (product.raw_data or {}).get('handle')
        # Collection-scoped Shopify URLs: {collection}/products/{handle}
        ok = f'{collection_path}/products/' in urlsplit(url).path
        if not ok and raw_handle:
            # Also accept shop-origin product URLs when shop_url was origin-normalized.
            ok = urlsplit(url).path.endswith(f'/products/{raw_handle}')
        (in_scope if ok else out_of_scope).append({
            'external_id': product.external_id,
            'product_url': url,
            'handle': raw_handle,
        })
    return {
        'collection_path': collection_path,
        'in_scope_count': len(in_scope),
        'out_of_scope_count': len(out_of_scope),
        'out_of_scope': out_of_scope[:20],
        'only_collection_products': len(out_of_scope) == 0 and len(products) > 0,
    }


def dedupe_report(products: list[Product], raw_products: list[dict]) -> dict:
    product_ids = [p.external_id for p in products if p.external_id]
    variant_ids = [v.variant_id for p in products for v in (p.variants or []) if v.variant_id]
    raw_product_ids = [str(p.get('id')) for p in raw_products if p.get('id') is not None]
    raw_variant_ids = [
        str(v.get('id'))
        for p in raw_products
        for v in (p.get('variants') or [])
        if isinstance(v, dict) and v.get('id') is not None
    ]
    return {
        'extracted_product_ids': len(product_ids),
        'unique_product_ids': len(set(product_ids)),
        'duplicate_product_ids': sorted(pid for pid, n in Counter(product_ids).items() if n > 1),
        'extracted_variant_ids': len(variant_ids),
        'unique_variant_ids': len(set(variant_ids)),
        'duplicate_variant_ids': sorted(vid for vid, n in Counter(variant_ids).items() if n > 1),
        'source_unique_product_ids': len(set(raw_product_ids)),
        'source_unique_variant_ids': len(set(raw_variant_ids)),
        'product_ids_match_source': set(product_ids) == set(raw_product_ids),
        'deduplicated_by_source_ids': (
            len(product_ids) == len(set(product_ids)) and len(variant_ids) == len(set(variant_ids))
        ),
    }


def compare_product(extracted: Product, source: dict) -> dict:
    source_variants = [v for v in (source.get('variants') or []) if isinstance(v, dict)]
    source_images = [img for img in (source.get('images') or []) if isinstance(img, dict) and img.get('src')]
    first = source_variants[0] if source_variants else {}
    source_desc = _strip_html(source.get('body_html') or '')
    extracted_desc = extracted.description or ''
    source_compare = first.get('compare_at_price') or None
    if source_compare in ('', '0.00', '0'):
        source_compare = None

    variant_checks = []
    by_id = {str(v.get('id')): v for v in source_variants if v.get('id') is not None}
    for variant in extracted.variants or []:
        src = by_id.get(variant.variant_id or '')
        if src is None:
            variant_checks.append({'variant_id': variant.variant_id, 'match': False, 'reason': 'missing_in_source'})
            continue
        options = {f'option{i}': src.get(f'option{i}') for i in range(1, 4) if src.get(f'option{i}')}
        published_image = None
        if isinstance(src.get('featured_image'), dict):
            published_image = src['featured_image'].get('src') or None
        published_image_id = str(src['image_id']) if src.get('image_id') is not None else None
        variant_checks.append({
            'variant_id': variant.variant_id,
            'title_match': (variant.title or '') == str(src.get('title') or ''),
            'price_match': _money(variant.price) == _money(src.get('price')),
            'sku_match': (variant.sku or None) == (src.get('sku') or None),
            'sku_extracted': variant.sku,
            'sku_source': src.get('sku') or None,
            'options_match': dict(variant.attributes or {}) == options,
            'options_extracted': dict(variant.attributes or {}),
            'options_source': options,
            'featured_image_match': variant.featured_image == published_image,
            'featured_image_id_match': variant.featured_image_id == published_image_id,
            'source_variant_image': published_image,
            'extracted_variant_image': variant.featured_image,
        })

    return {
        'external_id': extracted.external_id,
        'handle': source.get('handle'),
        'title_match': extracted.title == source.get('title'),
        'description_match': extracted_desc == source_desc,
        'price_match': _money(extracted.price) == _money(first.get('price')),
        'compare_at_price_match': _money(extracted.compare_at_price) == _money(source_compare),
        'sku_match': extracted.sku == (first.get('sku') or None),
        'currency_extracted': extracted.currency,
        'image_match': (extracted.image_url or None) == (source_images[0]['src'] if source_images else None),
        'additional_image_count_match': len(extracted.additional_images or []) == max(0, len(source_images) - 1),
        'variant_count_match': len(extracted.variants or []) == len(source_variants),
        'variant_checks': variant_checks,
        'missing_source_fields_left_null': {
            'compare_at_price_null_when_absent': (
                extracted.compare_at_price is None if not source_compare else True
            ),
            'sku_null_when_absent': (
                extracted.sku is None if not first.get('sku') else True
            ),
        },
    }


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)

    source = await fetch_source_catalog(COLLECTION_URL)
    discovered = len(source['products'])

    # Direct extractor run (collection-scoped URL) for pagination metadata.
    extractor_result = await ShopifyExtractor().extract(COLLECTION_URL, max_products=MAX_PRODUCTS)

    # Pipeline extract with the same supported budget (not the former smoke value of 5).
    result = await shopextract.extract(
        COLLECTION_URL,
        platform=Platform.SHOPIFY,
        max_urls=MAX_PRODUCTS,
        shop_url=COLLECTION_URL,
    )

    records = [product_record(p) for p in result.products]
    PRODUCTS_JSON.write_text(json.dumps({
        'collection_url': COLLECTION_URL,
        'extracted_at': started.isoformat(),
        'product_count': len(records),
        'products': records,
    }, indent=2, ensure_ascii=False), encoding='utf-8')

    scope = assert_collection_scope(result.products, COLLECTION_URL)
    dedupe = dedupe_report(result.products, extractor_result.products)

    source_by_id = {str(p.get('id')): p for p in source['products'] if p.get('id') is not None}
    sample = []
    for product in result.products[:3]:
        source_product = source_by_id.get(product.external_id or '')
        if source_product is None:
            sample.append({'external_id': product.external_id, 'match': False, 'reason': 'missing_in_source'})
        else:
            sample.append(compare_product(product, source_product))

    variant_count = sum(len(p.variants or []) for p in result.products)
    image_count = sum(1 + len(p.additional_images or []) for p in result.products)
    failed = max(0, discovered - result.product_count)

    # Completeness: only true when extractor exhausted pagination without a budget stop.
    completeness = {
        'extractor_complete': extractor_result.complete,
        'extractor_completeness_reason': extractor_result.completeness_reason,
        'extractor_pages_completed': extractor_result.pages_completed,
        'extractor_error': extractor_result.error,
        'pipeline_catalog_complete': result.catalog_complete,
        'pipeline_incompleteness_reasons': list(result.incompleteness_reasons),
        'pipeline_errors': list(result.errors),
        'source_end_reason': source['end_reason'],
        'source_pages_completed': source['pages_completed'],
        'requests_succeeded_alone_not_sufficient': True,
        'confirmed_end': bool(
            extractor_result.complete
            and extractor_result.completeness_reason is None
            and source['end_reason'] in {'empty_page', 'short_final_page'}
            and 'product_budget_reached' not in result.incompleteness_reasons
            and 'page_budget_reached' not in (extractor_result.completeness_reason or '',)
        ),
    }

    report = {
        'collection_url': COLLECTION_URL,
        'shop_origin': SHOP_ORIGIN,
        'started_at': started.isoformat(),
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'configuration': {
            'max_products': MAX_PRODUCTS,
            'former_smoke_budget_removed': True,
            'note': 'max_products/max_urls=1000 is the supported CLI-compatible budget; collection end is confirmed by short/empty page, not by request success alone.',
        },
        'counts': {
            'discovered': discovered,
            'extracted': result.product_count,
            'failed': failed,
            'variants': variant_count,
            'images': image_count,
            'raw_extractor_products': len(extractor_result.products),
        },
        'collection_scope': scope,
        'deduplication': dedupe,
        'completeness': completeness,
        'field_preservation_notes': {
            'description': 'from body_html with HTML stripped',
            'currency': 'from variant price_currency / currency / cart_currency cookie',
            'price': 'product price from variants[0].price; each variant keeps own price',
            'compare_at_price': 'product-level from variants[0].compare_at_price; null when absent/zero; not modeled per variant',
            'sku': 'product sku from variants[0].sku; variant.sku from source sku; null when absent',
            'options': 'variant.attributes option1..option3 when published',
            'images': 'product image_url + additional_images from images[]; variant featured_image/featured_image_id preserved when published else null',
        },
        'sample_source_comparisons': sample,
        'artifacts': {
            'products_json': str(PRODUCTS_JSON),
            'report_json': str(REPORT_JSON),
        },
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({
        'discovered': discovered,
        'extracted': result.product_count,
        'failed': failed,
        'variants': variant_count,
        'images': image_count,
        'only_collection_products': scope['only_collection_products'],
        'deduplicated_by_source_ids': dedupe['deduplicated_by_source_ids'],
        'confirmed_end': completeness['confirmed_end'],
        'catalog_complete': result.catalog_complete,
        'incompleteness_reasons': result.incompleteness_reasons,
        'products_json': str(PRODUCTS_JSON),
        'report_json': str(REPORT_JSON),
    }, indent=2))

    if result.product_count == 0:
        raise SystemExit(1)
    if not scope['only_collection_products']:
        raise SystemExit(2)
    if not dedupe['deduplicated_by_source_ids']:
        raise SystemExit(3)


if __name__ == '__main__':
    asyncio.run(main())
